export class RemotionCliError extends Error {}
export function projectRoot(): string;
export function resolveRemotionBin(root?: string): string;
export interface SpawnResult {
  status?: number | null;
  error?: Error;
}
export type SpawnImpl = (
  bin: string,
  args: string[],
  opts: { stdio?: unknown; shell?: boolean; cwd?: string },
) => SpawnResult;
export function runRemotion(
  args: string[],
  opts?: { root?: string; spawnImpl?: SpawnImpl },
): number;
