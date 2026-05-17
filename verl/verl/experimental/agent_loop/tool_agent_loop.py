# Copyright 2025 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import asyncio
import errno
import json
import logging
import os
import shutil
import stat
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import torch
from PIL import Image

from verl.experimental.agent_loop.agent_loop import (
    AgentLoopBase,
    AgentLoopOutput,
    register,
)
from verl.experimental.agent_loop.tool_parser import TOOL_PARSER_ERROR_FUNCTION_NAME, FunctionCall, ToolParser
from verl.experimental.agent_loop.utils import build_gpt_oss_tool_response_text
from verl.tools.schemas import ToolResponse
from verl.tools.utils.tool_registry import initialize_tools_from_config
from verl.utils.paths import get_spreadsheet_rl_data_root, normalize_thread_dir, normalize_workspace_id
from verl.utils.profiler import simple_timer
from verl.utils.rollout_trace import rollout_trace_op
from verl.workers.rollout.replica import TokenOutput

logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))

_WORKSPACE_MANIFEST_VERSION = 1


def _resolve_safe_data_file(root: Path, relpath: Path) -> Optional[Path]:
    if not relpath.parts or relpath.is_absolute() or any(part == ".." for part in relpath.parts):
        return None
    try:
        root_resolved = root.resolve(strict=True)
        candidate = root / relpath
        resolved = candidate.resolve(strict=True)

        # Reject leaf symlink targets.
        if candidate.is_symlink():
            return None

        # Allow only the first path segment to be a symlink (intentional top-level dataset links);
        # reject symlinks on deeper path components.
        first_is_symlink = False
        cursor = root
        for idx, part in enumerate(relpath.parts):
            cursor = cursor / part
            if cursor.is_symlink():
                if idx == 0:
                    first_is_symlink = True
                    continue
                return None

        if resolved.is_relative_to(root_resolved):
            return resolved if resolved.is_file() else None

        if first_is_symlink:
            first_root_resolved = (root / relpath.parts[0]).resolve(strict=True)
            if resolved.is_relative_to(first_root_resolved):
                return resolved if resolved.is_file() else None
    except (OSError, RuntimeError):
        return None
    return None


def _ensure_workspace_writable(workspace_dir: Path, workbook_path: Optional[Path] = None) -> None:
    try:
        os.chmod(workspace_dir, 0o777)
    except OSError:
        pass
    if workbook_path is None:
        return
    try:
        os.chmod(workbook_path, 0o666)
    except OSError:
        pass


def _open_lockfile(lock_path: Path):
    flags = os.O_RDWR | os.O_CREAT
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    else:
        try:
            if lock_path.is_symlink():
                raise RuntimeError(f"lock file path is a symlink: {lock_path}")
        except OSError:
            pass

    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RuntimeError(f"lock file path is a symlink: {lock_path}") from None
        raise RuntimeError(f"failed to open lock file: {exc}") from None

    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise RuntimeError(f"failed to stat lock file: {exc}") from None
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise RuntimeError(f"lock file is not a regular file: {lock_path}") from None

    return os.fdopen(fd, "r+", encoding="utf-8", errors="replace")


def _atomic_copy_file(src: Path, dest: Path) -> None:
    tmp_dest = dest.with_suffix(dest.suffix + f".tmp.{os.getpid()}.{uuid4().hex}")
    try:
        try:
            if tmp_dest.is_symlink() or tmp_dest.is_file():
                tmp_dest.unlink()
            elif tmp_dest.is_dir():
                shutil.rmtree(tmp_dest)
        except OSError:
            pass

        shutil.copy2(src, tmp_dest)
        os.replace(tmp_dest, dest)
    finally:
        try:
            if tmp_dest.is_symlink() or tmp_dest.is_file():
                tmp_dest.unlink()
            elif tmp_dest.is_dir():
                shutil.rmtree(tmp_dest)
        except OSError:
            pass


def _normalize_primary_ext(extra_info: Any) -> str:
    return ".xlsx"


def _prepare_sheet_arena_workspace(*, workspace_id: str, thread_dir: str, primary_ext: str) -> None:
    workspace_id = normalize_workspace_id(workspace_id)
    if workspace_id is None:
        return

    data_root = get_spreadsheet_rl_data_root()
    if not data_root.is_dir():
        return

    requested_rel = f"{thread_dir}/output{primary_ext}"
    requested_paths = [requested_rel]
    requested_rel_lower = requested_rel.lower()
    wants_flat_seed = requested_rel_lower.endswith("/output.xlsx") or requested_rel_lower == "output.xlsx"
    dest_rel = Path("data.xlsx") if wants_flat_seed else Path(requested_rel)

    workspace_root = data_root / "_workspaces"
    locks_dir = workspace_root / ".locks"
    workspace_dir = workspace_root / workspace_id

    for path in (locks_dir, workspace_dir):
        try:
            if path.is_symlink():
                raise RuntimeError(f"workspace preparation refused for symlink path: {path}")
        except OSError as exc:
            raise RuntimeError(f"workspace preparation failed checking path: {path}") from exc

    locks_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    lock_path = locks_dir / f"{workspace_id}.lock"
    manifest_path = locks_dir / f"{workspace_id}.manifest.json"

    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError("workspace preparation requires fcntl (Unix-only)") from exc

    lock_f = _open_lockfile(lock_path)
    try:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)

        dest_path = workspace_dir / dest_rel

        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest_paths = manifest.get("workspace_paths")
                manifest_version = manifest.get("version")
                if (
                    manifest_version == _WORKSPACE_MANIFEST_VERSION
                    and isinstance(manifest_paths, list)
                    and all(isinstance(p, str) for p in manifest_paths)
                ):
                    manifest_paths = sorted(manifest_paths)
                    if manifest_paths != sorted(requested_paths):
                        raise RuntimeError(
                            f"workspace_id {workspace_id!r} already prepared with different workspace_paths"
                        )
                    if dest_path.is_file() and not dest_path.is_symlink():
                        _ensure_workspace_writable(workspace_dir, dest_path)
                        return
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass

        if not dest_path.is_file() or dest_path.is_symlink():
            candidates = [requested_rel]
            try:
                base, ext = os.path.splitext(requested_rel)
                if ext:
                    candidates.append(base + ext.lower())
            except Exception:
                pass

            src_path = None
            for cand in candidates:
                src_path = _resolve_safe_data_file(data_root, Path(cand))
                if src_path is not None:
                    break
            if src_path is None:
                raise FileNotFoundError(f"missing seed workbook (tried: {candidates[:3]})")

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if dest_path.is_symlink() or dest_path.is_file():
                    dest_path.unlink()
                elif dest_path.is_dir():
                    shutil.rmtree(dest_path)
            except OSError:
                pass

            _atomic_copy_file(src_path, dest_path)
            _ensure_workspace_writable(workspace_dir, dest_path)
        else:
            _ensure_workspace_writable(workspace_dir, dest_path)

        tmp_manifest_path = Path(f"{manifest_path}.tmp.{os.getpid()}.{uuid4().hex}")
        try:
            with open(tmp_manifest_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "version": _WORKSPACE_MANIFEST_VERSION,
                        "workspace_paths": requested_paths,
                    },
                    f,
                    sort_keys=True,
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_manifest_path, manifest_path)
        finally:
            try:
                if tmp_manifest_path.exists():
                    tmp_manifest_path.unlink()
            except OSError:
                pass
    finally:
        try:
            lock_f.close()
        except OSError:
            pass


class AgentState(Enum):
    PENDING = "pending"
    GENERATING = "generating"
    PROCESSING_TOOLS = "processing_tools"
    TERMINATED = "terminated"


class AgentData:
    """Encapsulates all state variables for the agent loop. AgentData is passed to tool calling in case that
    tool may need to access full history state. User can store any tool session data in `extra_fields`."""

    def __init__(
        self,
        messages: list[dict[str, Any]],
        image_data: list[Image.Image],
        video_data: list[tuple[torch.Tensor, dict[str, Any]]],
        metrics: dict[str, Any],
        request_id: str,
        tools_kwargs: dict[str, Any],
        data_source: Optional[str] = None,
        ground_truth: Any = None,
        extra_info: Optional[dict[str, Any]] = None,
    ):
        self.messages = messages
        self.image_data = image_data
        self.video_data = video_data
        self.metrics = metrics
        self.request_id = request_id
        self.tools_kwargs = tools_kwargs
        self.data_source = data_source
        self.ground_truth = ground_truth
        self.extra_info = extra_info if isinstance(extra_info, dict) else {}

        # State variables
        self.prompt_ids: list[int] = []
        self.response_ids: list[int] = []
        self.response_mask: list[int] = []
        self.response_logprobs: list[float] = []
        self.turn_scores: list[float] = []
        self.tool_rewards: list[float] = []
        self.user_turns = 0
        self.assistant_turns = 0

        # Temporary state for tool calls
        self.tool_calls: list[FunctionCall] = []

        self.routed_experts = None

        # Extra fields for dynamic addition, e.g., tool session data
        self.extra_fields: dict[str, Any] = {}


@register("tool_agent")
class ToolAgentLoop(AgentLoopBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Initialize tools from config file
        self.max_user_turns = self.rollout_config.multi_turn.max_user_turns
        self.max_assistant_turns = self.rollout_config.multi_turn.max_assistant_turns
        self.max_parallel_calls = self.rollout_config.multi_turn.max_parallel_calls
        self.max_tool_response_length = self.rollout_config.multi_turn.max_tool_response_length
        self.tool_response_truncate_side = self.rollout_config.multi_turn.tool_response_truncate_side
        tool_config_path = self.rollout_config.multi_turn.tool_config_path
        tool_list = initialize_tools_from_config(tool_config_path) if tool_config_path else []
        self.tools = {tool.name: tool for tool in tool_list}
        if TOOL_PARSER_ERROR_FUNCTION_NAME in self.tools:
            raise ValueError(f"Tool name '{TOOL_PARSER_ERROR_FUNCTION_NAME}' is reserved for parser error feedback.")
        self.tool_schemas = [tool.tool_schema.model_dump(exclude_unset=True, exclude_none=True) for tool in tool_list]
        self.tool_parser = ToolParser.get_tool_parser(self.rollout_config.multi_turn.format, self.tokenizer)
        self.tool_parser_name = self.rollout_config.multi_turn.format

        self.prompt_length = self.rollout_config.prompt_length
        self.response_length = self.rollout_config.response_length

    @rollout_trace_op
    async def run(self, sampling_params: dict[str, Any], **kwargs) -> AgentLoopOutput:
        messages = list(kwargs["raw_prompt"])

        # extract images and videos from messages
        multi_modal_data = await self.process_vision_info(messages)
        images = multi_modal_data.get("images")
        videos = multi_modal_data.get("videos")

        metrics = {}
        request_id = uuid4().hex
        tools_kwargs = kwargs.get("tools_kwargs", {})
        data_source = kwargs.get("data_source", None)
        reward_model = kwargs.get("reward_model", {}) if isinstance(kwargs.get("reward_model", {}), dict) else {}
        ground_truth = reward_model.get("ground_truth", None)
        extra_info_raw = kwargs.get("extra_info", {})
        extra_info = dict(extra_info_raw) if isinstance(extra_info_raw, dict) else {}

        agent_data = AgentData(
            messages=messages,
            image_data=images,
            video_data=videos,
            metrics=metrics,
            request_id=request_id,
            tools_kwargs=tools_kwargs,
            data_source=data_source,
            ground_truth=ground_truth,
            extra_info=extra_info,
        )

        # Per-sample tool selection: filter global tools by extra_info.tool_selection
        tool_selection = extra_info.get("tool_selection")
        if tool_selection and self.tools:
            selected = {name: self.tools[name] for name in tool_selection if name in self.tools}
            agent_data._active_tools = selected
            agent_data._active_tool_schemas = [
                t.tool_schema.model_dump(exclude_unset=True, exclude_none=True) for t in selected.values()
            ]
        else:
            agent_data._active_tools = self.tools
            agent_data._active_tool_schemas = self.tool_schemas

        if data_source == "sheet_arena":
            thread_dir = normalize_thread_dir(ground_truth)
            primary_ext = _normalize_primary_ext(extra_info)
            extra_info["primary_ext"] = primary_ext
            extra_info["sandbox_workspace_id"] = request_id
            if thread_dir is not None:
                try:
                    await asyncio.to_thread(
                        _prepare_sheet_arena_workspace,
                        workspace_id=request_id,
                        thread_dir=thread_dir,
                        primary_ext=primary_ext,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to prepare sheet_arena workspace (workspace_id=%s, thread_dir=%s): %s",
                        request_id,
                        thread_dir,
                        e,
                    )

        # State machine loop
        state = AgentState.PENDING
        while state != AgentState.TERMINATED:
            if state == AgentState.PENDING:
                state = await self._handle_pending_state(agent_data, sampling_params)
            elif state == AgentState.GENERATING:
                state = await self._handle_generating_state(agent_data, sampling_params)
            elif state == AgentState.PROCESSING_TOOLS:
                state = await self._handle_processing_tools_state(agent_data)
            else:
                logger.error(f"Invalid state: {state}")
                state = AgentState.TERMINATED

        # Finalize output
        response_ids = agent_data.prompt_ids[-len(agent_data.response_mask) :]
        prompt_ids = agent_data.prompt_ids[: len(agent_data.prompt_ids) - len(agent_data.response_mask)]
        multi_modal_data = {}
        if agent_data.image_data is not None:
            multi_modal_data["images"] = agent_data.image_data
        if agent_data.video_data is not None:
            multi_modal_data["videos"] = agent_data.video_data

        output: AgentLoopOutput = AgentLoopOutput(
            prompt_ids=prompt_ids,
            response_ids=response_ids[: self.response_length],
            response_mask=agent_data.response_mask[: self.response_length],
            multi_modal_data=multi_modal_data,
            response_logprobs=agent_data.response_logprobs[: self.response_length]
            if agent_data.response_logprobs
            else None,
            num_turns=agent_data.user_turns + agent_data.assistant_turns + 1,
            metrics=agent_data.metrics,
            routed_experts=(
                agent_data.routed_experts[: len(prompt_ids) + self.response_length]
                if agent_data.routed_experts is not None
                else None
            ),
            extra_fields=agent_data.extra_fields,
        )
        output.extra_fields.update(
            {
                "turn_scores": agent_data.turn_scores,
                "tool_rewards": agent_data.tool_rewards,
                "sandbox_workspace_id": agent_data.request_id,
            }
        )
        return output

    async def _handle_pending_state(self, agent_data: AgentData, sampling_params: dict[str, Any]) -> AgentState:
        """Handle the pending state: prepare the prompt and start generation."""
        schemas = getattr(agent_data, "_active_tool_schemas", self.tool_schemas)
        prompt_ids = await self.apply_chat_template(
            agent_data.messages,
            tools=schemas,
            images=agent_data.image_data,
            videos=agent_data.video_data,
        )
        agent_data.prompt_ids = prompt_ids
        return AgentState.GENERATING

    async def _handle_generating_state(
        self, agent_data: AgentData, sampling_params: dict[str, Any], ignore_termination: bool = False
    ) -> AgentState:
        """Handle the generating state: generate model response and check for tool calls."""
        with simple_timer("generate_sequences", agent_data.metrics):
            output: TokenOutput = await self.server_manager.generate(
                request_id=agent_data.request_id,
                prompt_ids=agent_data.prompt_ids,
                sampling_params=sampling_params,
                image_data=agent_data.image_data,
                video_data=agent_data.video_data,
            )
        # first time to set num_preempted
        if agent_data.metrics.get("num_preempted") is None:
            agent_data.metrics["num_preempted"] = output.num_preempted if output.num_preempted is not None else -1
        # then add num_preempted to the metrics
        else:
            agent_data.metrics["num_preempted"] += output.num_preempted if output.num_preempted is not None else 0

        if not agent_data.extra_fields:
            agent_data.extra_fields.update(output.extra_fields)
        else:
            # Multi-round calls, only update the maximum max_global_steps.
            max_global_steps = output.extra_fields.get("max_global_steps", None)
            if max_global_steps:
                agent_data.extra_fields["max_global_steps"] = max_global_steps

        agent_data.assistant_turns += 1
        agent_data.response_ids = output.token_ids
        agent_data.prompt_ids += agent_data.response_ids
        agent_data.response_mask += [1] * len(agent_data.response_ids)
        if output.log_probs:
            agent_data.response_logprobs += output.log_probs

        if output.routed_experts is not None:
            agent_data.routed_experts = output.routed_experts

        # Check termination conditions
        if not ignore_termination and len(agent_data.response_mask) >= self.response_length:
            return AgentState.TERMINATED
        if self.max_assistant_turns and agent_data.assistant_turns >= self.max_assistant_turns:
            return AgentState.TERMINATED
        if self.max_user_turns and agent_data.user_turns >= self.max_user_turns:
            return AgentState.TERMINATED

        # Extract tool calls (use per-sample tools if routed)
        active_tools = getattr(agent_data, "_active_tools", self.tools)
        tools = [tool.tool_schema for tool in active_tools.values()]
        _, agent_data.tool_calls = await self.tool_parser.extract_tool_calls(agent_data.response_ids, tools)

        if agent_data.tool_calls:
            return AgentState.PROCESSING_TOOLS
        else:
            return AgentState.TERMINATED

    async def _handle_processing_tools_state(self, agent_data: AgentData) -> AgentState:
        """Handle the processing tools state: execute tool calls and prepare tool responses."""
        add_messages: list[dict[str, Any]] = []
        new_images_this_turn: list[Any] = []  # Local variable instead of agent_data attribute

        tasks = []
        tool_call_names = []
        for tool_call in agent_data.tool_calls[: self.max_parallel_calls]:
            tasks.append(self._call_tool(tool_call, agent_data.tools_kwargs, agent_data))
            tool_call_names.append(tool_call.name)

        with simple_timer("tool_calls", agent_data.metrics):
            responses = await asyncio.gather(*tasks)

        # Process tool responses and update multi_modal_data
        # Removed: agent_data.new_images_this_turn = []
        for tool_response, tool_reward, _ in responses:
            # Create message from tool response
            if tool_response.image or tool_response.video:
                # Multi-modal content with structured format
                if not getattr(self.processor, "image_processor", None):
                    raise ValueError(
                        "Multimedia data can only be processed by `processor`, but the processor is None. "
                        "This error is often caused if you are using a LLM model but your tool returns multimodal "
                        "data. Plase use a vlm as the base model."
                    )
                content = []
                if tool_response.image:
                    content.append({"type": "image"})
                if tool_response.video:
                    content.append({"type": "video"})
                if tool_response.text:
                    content.append({"type": "text", "text": tool_response.text})
                message = {"role": "tool", "content": content}
            else:
                # Text-only content
                message = {"role": "tool", "content": tool_response.text or ""}

            add_messages.append(message)

            # Handle image data
            if tool_response.image:
                # Add new image data
                if isinstance(tool_response.image, list):
                    # Ensure all elements in the list are valid image objects
                    for img in tool_response.image:
                        if img is not None:  # Add a check to ensure the image is not None
                            new_images_this_turn.append(img)  # Using local variable
                else:
                    # Ensure the image is not None
                    if tool_response.image is not None:
                        new_images_this_turn.append(tool_response.image)  # Using local variable

            # Handle video data
            if tool_response.video:
                # Currently not supported, raise informative error
                logger.warning("Multimedia type 'video' is not currently supported. Only 'image' is supported.")
                raise NotImplementedError(
                    "Multimedia type 'video' is not currently supported. Only 'image' is supported."
                )

            if tool_reward is not None:
                agent_data.tool_rewards.append(tool_reward)

        agent_data.messages.extend(add_messages)

        if self.tool_parser_name == "gpt-oss":
            logger.info("manually format tool responses for gpt-oss")
            tool_response_text = build_gpt_oss_tool_response_text(add_messages, tool_call_names)
            response_ids = await self.loop.run_in_executor(
                None, lambda: self.tokenizer.encode(tool_response_text, add_special_tokens=False)
            )
        else:
            # Note that we have to pass None to the images and videos if there are no new images / videos
            # to stay compatible with downstream image processing logic!
            images = new_images_this_turn if new_images_this_turn else None
            videos = None
            response_ids = await self.apply_chat_template(
                add_messages,
                images=images,
                videos=videos,
                remove_system_prompt=True,
            )

        if len(agent_data.response_mask) + len(response_ids) >= self.response_length:
            return AgentState.TERMINATED
        # Update prompt_ids and response_mask

        if new_images_this_turn:
            if agent_data.image_data is None:
                agent_data.image_data = []
            elif not isinstance(agent_data.image_data, list):
                agent_data.image_data = [agent_data.image_data]
            for img in new_images_this_turn:
                agent_data.image_data.append(img)

        agent_data.prompt_ids += response_ids
        agent_data.response_mask += [0] * len(response_ids)
        if agent_data.response_logprobs:
            agent_data.response_logprobs += [0.0] * len(response_ids)
        agent_data.user_turns += 1
        return AgentState.GENERATING

    async def _call_tool(
        self, tool_call: FunctionCall, tools_kwargs: dict[str, Any], agent_data: AgentData
    ) -> tuple[ToolResponse, float | None, dict]:
        """Call tool and return tool response."""
        active_tools = getattr(agent_data, "_active_tools", self.tools)

        # Validate tool name
        tool_name = tool_call.name
        if tool_name == TOOL_PARSER_ERROR_FUNCTION_NAME and getattr(tool_call, "internal", False):
            try:
                tool_args = json.loads(tool_call.arguments)
            except (json.JSONDecodeError, TypeError):
                tool_args = {}
            message = tool_args.get("message") if isinstance(tool_args, dict) else None
            tool_response_text = str(message or "Tool call parse error.")
            if tool_response_text and len(tool_response_text) > self.max_tool_response_length:
                if self.tool_response_truncate_side == "left":
                    tool_response_text = tool_response_text[: self.max_tool_response_length] + "...(truncated)"
                elif self.tool_response_truncate_side == "right":
                    tool_response_text = "(truncated)..." + tool_response_text[-self.max_tool_response_length :]
                else:
                    length = self.max_tool_response_length // 2
                    tool_response_text = (
                        tool_response_text[:length] + "...(truncated)..." + tool_response_text[-length:]
                    )
            return ToolResponse(text=tool_response_text), None, {}

        if tool_name not in active_tools:
            available = list(active_tools.keys())
            msg = f"Unknown function '{tool_name}'. Available tools: {available}"
            logger.warning(msg)
            return ToolResponse(text=msg), 0.0, {}

        # Validate tool arguments
        try:
            tool_args = json.loads(tool_call.arguments)
        except (json.JSONDecodeError, TypeError) as e:
            msg = f"Invalid JSON in arguments for '{tool_name}': {e}"
            logger.warning(msg)
            return ToolResponse(text=msg), 0.0, {}

        # Execute tool
        tool, instance_id = None, None
        try:
            tool = active_tools[tool_name]
            kwargs = tools_kwargs.get(tool_name, {})
            instance_id, _ = await tool.create(create_kwargs=kwargs.get("create_kwargs", {}))
            tool_execution_response, tool_reward, res = await tool.execute(
                instance_id,
                tool_args,
                agent_data=agent_data,
                workspace_id=agent_data.request_id,
                data_source=agent_data.data_source,
                ground_truth=agent_data.ground_truth,
                extra_info=agent_data.extra_info,
            )
        except Exception as e:
            logger.warning(f"Error executing tool '{tool_name}': {e}")
            return ToolResponse(text=f"Error executing tool '{tool_name}': {e}"), 0.0, {}
        finally:
            if tool and instance_id:
                await tool.release(instance_id)

        tool_response_text = tool_execution_response.text
        if tool_response_text and len(tool_response_text) > self.max_tool_response_length:
            if self.tool_response_truncate_side == "left":
                tool_response_text = tool_response_text[: self.max_tool_response_length] + "...(truncated)"
            elif self.tool_response_truncate_side == "right":
                tool_response_text = "(truncated)..." + tool_response_text[-self.max_tool_response_length :]
            else:
                length = self.max_tool_response_length // 2
                tool_response_text = tool_response_text[:length] + "...(truncated)..." + tool_response_text[-length:]

        # Create ToolResponse from tool execution result
        tool_response_kwargs = {"text": tool_response_text}

        # Add multimedia data if present
        for attr_name in ["image", "video"]:
            if hasattr(tool_execution_response, attr_name):
                attr_value = getattr(tool_execution_response, attr_name)
                if attr_value is not None:
                    tool_response_kwargs[attr_name] = attr_value

        return ToolResponse(**tool_response_kwargs), tool_reward, res
