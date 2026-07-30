import { createReadStream, createWriteStream } from "node:fs";
import { readdir, stat, unlink } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { pipeline } from "node:stream/promises";
import { createGzip } from "node:zlib";
import { build } from "vite";

const configFile = fileURLToPath(new URL("../frontend/vite.config.ts", import.meta.url));
const assetDirectory = fileURLToPath(new URL("../dist/assets/", import.meta.url));
const compressedFile = `${assetDirectory}/mog-wasm.wasm.gz`;

await build({ configFile });

const wasmFiles = (await readdir(assetDirectory)).filter((name) => name.endsWith(".wasm"));
if (wasmFiles.length !== 1) {
  throw new Error(`Expected one emitted Mog WASM asset, found ${wasmFiles.length}.`);
}

const sourceFile = `${assetDirectory}/${wasmFiles[0]}`;
const sourceStats = await stat(sourceFile);
if (sourceStats.size <= 25 * 1024 * 1024) {
  throw new Error(`Expected the Mog WASM asset to exceed 25 MiB, found ${sourceStats.size} bytes.`);
}

await pipeline(createReadStream(sourceFile), createGzip({ level: 9 }), createWriteStream(compressedFile));
await unlink(sourceFile);

const compressedStats = await stat(compressedFile);
if (compressedStats.size > 25 * 1024 * 1024) {
  throw new Error(`Compressed Mog WASM still exceeds 25 MiB: ${compressedStats.size} bytes.`);
}
console.log(
  `Compressed Mog WASM from ${sourceStats.size} to ${compressedStats.size} bytes in dist/assets.`,
);
