# Copyright 2024 Bytedance Ltd. and/or its affiliates
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
import os
import shutil
import sys
import time
from contextlib import asynccontextmanager
from typing import List, Optional

import aiofiles
import aiofiles.os
import structlog

from sandbox.utils.common import cached_context, random_cgroup_name

logger = structlog.stdlib.get_logger()


async def execute_command(cmd: List[str], raise_nonzero: bool = True):
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()

    if process.returncode != 0 and raise_nonzero:
        raise RuntimeError(f'Failed to execute {" ".join(cmd)}: {stdout.decode()}\n{stderr.decode()}')


async def mount_tmpfs(mount_point: str):
    mount_cmd = ['sudo', 'mount', '-t', 'tmpfs', 'tmpfs', mount_point]
    await execute_command(mount_cmd)


async def unmount_fs(mount_point: str):
    mount_cmd = ['sudo', 'umount', '-l', mount_point]
    await execute_command(mount_cmd)


@asynccontextmanager
async def tmp_overlayfs():
    base_dir = f'/tmp/overlay_{random_cgroup_name()}'
    merged_dir = f'{base_dir}/merged'
    tmpfs_dir = f'{base_dir}/tmpfs'
    upper_dir = f'{tmpfs_dir}/upper'
    work_dir = f'{tmpfs_dir}/work'
    for sub_dir in [tmpfs_dir, merged_dir]:
        await aiofiles.os.makedirs(sub_dir)
    # overlayfs requires work and upper dir to be non-overlayfs, see https://stackoverflow.com/questions/67198603/overlayfs-inside-docker-container
    await mount_tmpfs(tmpfs_dir)
    for sub_dir in [upper_dir, work_dir]:
        await aiofiles.os.makedirs(sub_dir)

    mount_cmd = [
        'sudo', 'mount', '-t', 'overlay', 'overlay', '-o', f'lowerdir=/,upperdir={upper_dir},workdir={work_dir}',
        merged_dir
    ]
    await execute_command(mount_cmd)
    await execute_command(['sudo', 'mount', '-t', 'proc', '/proc', f'{merged_dir}/proc'])
    await execute_command(['sudo', 'mount', '-t', 'sysfs', '/sys', f'{merged_dir}/sys'])
    await execute_command(['sudo', 'mount', '--rbind', '/dev', f'{merged_dir}/dev'])
    await execute_command(['cp', '/etc/hosts', f'{merged_dir}/etc/'])
    await execute_command(['cp', '/etc/resolv.conf', f'{merged_dir}/etc/'])

    yield merged_dir

    # TODO: cleanup errors shouldn't interrupt other cleanups
    await unmount_fs(f'{merged_dir}/dev')
    await unmount_fs(f'{merged_dir}/sys')
    await unmount_fs(f'{merged_dir}/proc')
    await unmount_fs(merged_dir)
    await unmount_fs(tmpfs_dir)
    await asyncio.to_thread(shutil.rmtree, base_dir)


async def cleanup_group(cg):
    try:
        with open(f'/sys/fs/cgroup/{cg.replace(":", "/")}/tasks', 'r') as f:
            pids = f.read().splitlines()

        for pid in pids:
            while True:
                await execute_command(['sudo', 'kill', '-9', pid], False)
                if not os.path.exists(f'/proc/{pid}'):
                    break
                await asyncio.sleep(1)

        await execute_command(['sudo', 'cgdelete', '-g', cg])
    except Exception as e:
        logger.error(f"Error cleaning up group {cg}: {e}")


@cached_context
@asynccontextmanager
async def tmp_cgroup(mem_limit: Optional[str] = None, cpu_limit: Optional[float] = None):
    '''
    mem_limit: in bytes, e.g. 4G
    cpu_limit: e.g. 0.5
    '''
    groups = []

    if mem_limit is None and cpu_limit is None:
        raise Exception('every resource is unlimited, no need for cgroup')

    if mem_limit is not None:
        mem_group_name = f'sandbox_mem_{random_cgroup_name()}'
        await execute_command(['sudo', 'cgcreate', '-g', f'memory:{mem_group_name}'])
        await execute_command(['sudo', 'cgset', '-r', f'memory.limit_in_bytes={mem_limit}', mem_group_name])
        groups.append(f'memory:{mem_group_name}')

    if cpu_limit is not None:
        cpu_group_name = f'sandbox_cpu_{random_cgroup_name()}'
        await execute_command(['sudo', 'cgcreate', '-g', f'cpu:{cpu_group_name}'])
        await execute_command(['sudo', 'cgset', '-r', f'cpu.cfs_quota_us={int(100000 * cpu_limit)}', cpu_group_name])
        await execute_command(['sudo', 'cgset', '-r', f'cpu.cfs_period_us=100000', cpu_group_name])
        groups.append(f'cpu:{cpu_group_name}')
    '''
    cpuset can make program use specifc cpu cores, which will improve performance for memory bound programs.
    this is not an important feature for now and thus disabled
    if cpu_list is not None:
        # NOTE: sandbox/cset_xxx style naming fails the cgset command
        cset_group_name = f'sandbox_cset_{random_cgroup_name()}'
        await execute_command(['sudo', 'cgcreate', '-g', f'cpuset:{cset_group_name}'])
        await execute_command(['sudo', 'cgset', '-r', f'cpuset.cpus={cpu_list}', cset_group_name])
        await execute_command(['sudo', 'cgset', '-r', f'cpuset.mems={get_memory_nodes()}', cset_group_name])
        # as this easily conflicts with other host cgroup settings,
        # we do not prevent other processes from using the same cpu for now
        # await execute_command(['sudo', 'cgset', '-r', 'cpuset.cpu_exclusive=1', cset_group_name])
        groups.append(f'cpuset:{cset_group_name}')
    '''

    yield groups

    for cg in groups:
        asyncio.create_task(cleanup_group(cg))


available_subnets = []
pytest_worker_id = os.environ.get("PYTEST_XDIST_WORKER")
if pytest_worker_id is not None:
    # pytest with multi-worker will cause conflicting ip subnet range
    for j in range(0, 256):
        available_subnets.append(f"172.{16 + int(pytest_worker_id[2:])}.{j}")
else:
    for i in range(16, 32):  # 172.16.x.x to 172.31.x.x
        for j in range(0, 256):  # 172.x.0.x to 172.x.255.x
            available_subnets.append(f"172.{i}.{j}")
create_netns_script = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../scripts/create_net_namespace.sh'))
clean_netns_script = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../scripts/clean_net_namespace.sh'))


# https://www.reddit.com/r/ProgrammerHumor/comments/8mdcde/the_utimate_dhcp_server/
def get_subnet_ip_rfc_2322():
    if len(available_subnets) == 0:
        logger.warning('all subnet ip used up')
        return None
    return available_subnets.pop()


def return_subnet_ip_rfc_2322(ip):
    available_subnets.append(ip)


@cached_context
@asynccontextmanager
async def tmp_netns(no_bridge: bool = False):
    net_ns_name = random_cgroup_name()
    while True:
        subnet_ip = get_subnet_ip_rfc_2322()
        if subnet_ip is not None:
            break
        await asyncio.sleep(0.5)
    args = [net_ns_name, subnet_ip]
    if no_bridge:
        args += ['--no-bridge']
    await execute_command(['sudo', create_netns_script] + args)
    yield net_ns_name
    await execute_command(['sudo', clean_netns_script] + args)
    return_subnet_ip_rfc_2322(subnet_ip)


async def main():
    begin = time.time()
    print(f'start: {begin}')
    async with tmp_overlayfs() as root, tmp_cgroup(mem_limit='4G', cpu_limit=0.5) as cgroups, tmp_netns() as netns:
        init = time.time()
        print(f'init finish: {init - begin}')
        prefix = []
        for cg in cgroups:
            prefix += ['cgexec', '-g', cg]
        chroot_cmd = ['chroot', root]
        # unshare_cmd = ['unshare', '--net', '--pid', '--fork', '--mount-proc']
        unshare_cmd = ['unshare', '--pid', '--fork', '--mount-proc']
        # TODO: mount other volumns per need. see https://superuser.com/questions/165116/mount-dev-proc-sys-in-a-chroot-environment
        final_cmd = prefix + chroot_cmd + ['bash', '-c', f'cd /tmp && {" ".join(sys.argv[1:])}']
        # final_cmd = prefix + chroot_cmd + unshare_cmd + ['bash', '-c', f'cd /tmp && echo $GFD']
        # final_cmd = prefix + chroot_cmd + unshare_cmd + ['bash', '-c', 'cd', '/tmp', '&&'] + sys.argv[1:]
        print(f'cmd: {" ".join(final_cmd)}')
        await execute_command(final_cmd)
        cmd = time.time()
        print(f'run command finish: {cmd - init}')
    teardown = time.time()
    print(f'teardown finish: {teardown - cmd}')


# asyncio.run(main())
