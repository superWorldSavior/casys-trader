export const BRIDGE_TIMEOUT_MS = 40_000;
export const BRIDGE_MAX_BYTES = 20 * 1024 * 1024;

export type BridgeResult =
  | { ok: true; body: string }
  | { ok: false; status: number; body: string };

export type RunCommandRequest = {
  command: string;
  args: string[];
  cwd: string;
  env: Record<string, string>;
  timeoutMs: number;
  maxBytes: number;
};

export type RunCommandResult = {
  code: number;
  stdout: Uint8Array;
  stderr: Uint8Array;
};

export type RunCommand = (
  request: RunCommandRequest,
) => Promise<RunCommandResult>;

export class BridgeTimeoutError extends Error {
  override name = "BridgeTimeoutError";
  constructor(public readonly timeoutMs: number) {
    super(`desk api timed out after ${timeoutMs}ms`);
  }
}

export class BridgeOutputTooLargeError extends Error {
  override name = "BridgeOutputTooLargeError";
  constructor(public readonly maxBytes: number) {
    super(`desk api output exceeded ${maxBytes} bytes`);
  }
}

function decoder(): TextDecoder {
  return new TextDecoder();
}

function pathEnv(current: string | undefined): string {
  const prefix = "/opt/homebrew/bin:/usr/local/bin";
  if (!current) return prefix;
  if (current.split(":").includes("/opt/homebrew/bin")) return current;
  return `${prefix}:${current}`;
}

function collectEnv(
  envGet: (key: string) => string | undefined,
): Record<string, string> {
  return { PATH: pathEnv(envGet("PATH")) };
}

function concatBytes(chunks: Uint8Array[]): Uint8Array {
  let total = 0;
  for (const chunk of chunks) total += chunk.byteLength;
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return out;
}

function killChild(child: Deno.ChildProcess): void {
  try {
    child.kill("SIGKILL");
  } catch {
    // already exited
  }
}

export async function runDenoCommand(
  request: RunCommandRequest,
): Promise<RunCommandResult> {
  let overflowed = false;
  let timedOut = false;
  const child = new Deno.Command(request.command, {
    args: request.args,
    cwd: request.cwd,
    env: request.env,
    stdout: "piped",
    stderr: "piped",
  }).spawn();
  const readers: ReadableStreamDefaultReader<Uint8Array>[] = [];

  const stop = () => {
    killChild(child);
    for (const reader of readers) {
      try {
        reader.cancel();
      } catch {
        // already closed
      }
    }
  };

  const timer = setTimeout(() => {
    timedOut = true;
    stop();
  }, request.timeoutMs);

  const collect = async (
    stream: ReadableStream<Uint8Array> | null,
  ): Promise<Uint8Array> => {
    if (!stream) return new Uint8Array();
    const reader = stream.getReader();
    readers.push(reader);
    const chunks: Uint8Array[] = [];
    let size = 0;
    while (true) {
      let readResult: ReadableStreamReadResult<Uint8Array>;
      try {
        readResult = await reader.read();
      } catch {
        break;
      }
      if (readResult.done || !readResult.value) break;
      if (overflowed || timedOut) break;
      if (size + readResult.value.byteLength > request.maxBytes) {
        overflowed = true;
        stop();
        break;
      }
      chunks.push(readResult.value);
      size += readResult.value.byteLength;
    }
    return concatBytes(chunks);
  };

  try {
    const [stdout, stderr, status] = await Promise.all([
      collect(child.stdout),
      collect(child.stderr),
      child.status,
    ]);
    if (overflowed) throw new BridgeOutputTooLargeError(request.maxBytes);
    if (timedOut) throw new BridgeTimeoutError(request.timeoutMs);
    return {
      code: status.code ?? -1,
      stdout,
      stderr,
    };
  } finally {
    clearTimeout(timer);
    stop();
  }
}

export type CreateUvBridgeOptions = {
  traderRoot: string;
  runCommand?: RunCommand;
  uvBin?: string;
  timeoutMs?: number;
  maxBytes?: number;
  envGet?: (key: string) => string | undefined;
};

export function createUvBridge(
  options: CreateUvBridgeOptions,
): (args: readonly string[]) => Promise<BridgeResult> {
  const timeoutMs = options.timeoutMs ?? BRIDGE_TIMEOUT_MS;
  const maxBytes = options.maxBytes ?? BRIDGE_MAX_BYTES;
  const envGet = options.envGet ?? ((key: string) => Deno.env.get(key));
  const uvBin = options.uvBin ?? envGet("UV_BIN") ?? "uv";
  const runCommand = options.runCommand ?? runDenoCommand;
  const inFlight = new Map<string, Promise<BridgeResult>>();

  return (args) => {
    const key = args.join("\0");
    const existing = inFlight.get(key);
    if (existing) return existing;

    const pending = (async (): Promise<BridgeResult> => {
      try {
        const result = await runCommand({
          command: uvBin,
          args: ["run", "python", "desktop/bridge/api.py", ...args],
          cwd: options.traderRoot,
          env: collectEnv(envGet),
          timeoutMs,
          maxBytes,
        });
        if (result.code !== 0) {
          const stderr = decoder().decode(result.stderr).trim();
          return {
            ok: false,
            status: 500,
            body: stderr || `desk api exited ${result.code}`,
          };
        }
        const body = decoder().decode(result.stdout);
        if (!body) {
          return {
            ok: false,
            status: 500,
            body: "desk api returned empty output",
          };
        }
        return { ok: true, body };
      } catch (error) {
        if (
          error instanceof BridgeTimeoutError ||
          error instanceof BridgeOutputTooLargeError
        ) {
          return { ok: false, status: 500, body: error.message };
        }
        const message = error instanceof Error ? error.message : String(error);
        return { ok: false, status: 500, body: message || "desk api failed" };
      }
    })();

    inFlight.set(key, pending);
    pending.finally(() => {
      if (inFlight.get(key) === pending) inFlight.delete(key);
    });
    return pending;
  };
}
