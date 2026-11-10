/**
 * Console commands waiting for their answer (the spinner in the command input).
 *
 * A command is answered by its `command_result`. A typed `stop` is answered first by a `notice`
 * with the command's `id` (backend apps/console/consumers.py `_stop`: the panel stops the server
 * instead, or refuses), followed by a `command_result` with the same id and the same text: the
 * notice ends the wait and is shown; the result that follows is not shown a second time.
 */
export class PendingCommands {
  private readonly waiting = new Map<string, string>();
  private readonly answeredByNotice = new Set<string>();

  get size(): number {
    return this.waiting.size;
  }

  add(id: string, command: string): void {
    this.waiting.set(id, command);
  }

  /** A notice; true when it answered a waiting command (the count changed). */
  notice(id: unknown): boolean {
    if (id === undefined || id === null || id === "") return false;
    const key = String(id);
    if (!this.waiting.delete(key)) return false;
    this.answeredByNotice.add(key);
    return true;
  }

  /** A command_result: "skip" when a notice already answered this command with the same text. */
  result(id: unknown): "show" | "skip" {
    const key = String(id);
    this.waiting.delete(key);
    return this.answeredByNotice.delete(key) ? "skip" : "show";
  }

  /** The connection dropped: the commands that got no answer (forgotten). */
  drop(): string[] {
    const left = [...this.waiting.values()];
    this.waiting.clear();
    this.answeredByNotice.clear();
    return left;
  }
}
