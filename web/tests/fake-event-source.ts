// A stand-in for EventSource that the tests drive by hand.
export class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static instances: FakeEventSource[] = [];

  readyState = FakeEventSource.CONNECTING;
  closed = false;
  private listeners = new Map<string, ((event: MessageEvent | Event) => void)[]>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent | Event) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }

  open(): void {
    this.readyState = FakeEventSource.OPEN;
    this.emit("open", new Event("open"));
  }

  fail(closed = false): void {
    this.readyState = closed ? FakeEventSource.CLOSED : FakeEventSource.CONNECTING;
    this.emit("error", new Event("error"));
  }

  send(type: string, data: unknown): void {
    this.readyState = FakeEventSource.OPEN;
    this.emit(type, new MessageEvent(type, { data: JSON.stringify(data) }));
  }

  private emit(type: string, event: MessageEvent | Event): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}
