// Undo/redo stack for the composition editor.
//
// Deliberately simple and immutable-friendly: `present` is the live value; every
// `commit` pushes the old present onto the undo stack and clears the redo stack.
// Bounded to avoid unbounded growth on long sessions.

export interface HistorySnapshot<T> {
  present: T;
  canUndo: boolean;
  canRedo: boolean;
  undoDepth: number;
  redoDepth: number;
}

export class History<T> {
  private past: T[] = [];
  private future: T[] = [];
  private _present: T;
  private readonly limit: number;

  constructor(initial: T, limit = 100) {
    this._present = initial;
    this.limit = Math.max(1, limit);
  }

  get present(): T {
    return this._present;
  }
  get canUndo(): boolean {
    return this.past.length > 0;
  }
  get canRedo(): boolean {
    return this.future.length > 0;
  }

  /** Commit a new present; the previous present becomes undoable. */
  commit(next: T): void {
    this.past.push(this._present);
    if (this.past.length > this.limit) this.past.shift();
    this._present = next;
    this.future = [];
  }

  /** Replace the present WITHOUT creating an undo entry (e.g. external reload). */
  reset(next: T): void {
    this._present = next;
    this.past = [];
    this.future = [];
  }

  undo(): T {
    const prev = this.past.pop();
    if (prev === undefined) return this._present;
    this.future.push(this._present);
    this._present = prev;
    return this._present;
  }

  redo(): T {
    const nxt = this.future.pop();
    if (nxt === undefined) return this._present;
    this.past.push(this._present);
    this._present = nxt;
    return this._present;
  }

  snapshot(): HistorySnapshot<T> {
    return {
      present: this._present,
      canUndo: this.canUndo,
      canRedo: this.canRedo,
      undoDepth: this.past.length,
      redoDepth: this.future.length,
    };
  }
}
