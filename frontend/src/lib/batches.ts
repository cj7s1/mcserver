/**
 * Client-side batching for requests the backend caps per call: Django refuses a multipart request
 * with more than 100 files (DATA_UPLOAD_MAX_NUMBER_FILES), and the content update endpoint takes at
 * most 100 file names. Splitting on the client works with old and new panels alike.
 */
export const BATCH_SIZE = 100;

/** Split `items` into consecutive batches of at most `size` (no empty batches). */
export function chunk<T>(items: readonly T[], size: number = BATCH_SIZE): T[][] {
  if (size < 1) throw new Error("batch size must be at least 1");
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

export interface BatchProgress {
  /** 1-based number of the batch being sent */
  batch: number;
  batches: number;
  /** items handled before this batch */
  done: number;
  total: number;
}

/** "batch 2 of 3 (files 101–200 of 250)"; empty for a single batch. */
export function batchLabel(p: BatchProgress | null | undefined, noun = "files"): string {
  if (!p || p.batches <= 1) return "";
  const from = p.done + 1;
  const to = Math.min(p.done + BATCH_SIZE, p.total);
  return `batch ${p.batch} of ${p.batches} (${noun} ${from}–${to} of ${p.total})`;
}
