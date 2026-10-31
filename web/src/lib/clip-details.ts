// A clip's details, asked for once for each version of its Library entry. A click opens the settings pane and the
// details panel together, and a Library refresh hands the panel back an unchanged copy of the clip: neither needs a
// new answer. Both panes show recorded facts, and the settings that change them change the entry too.

import { api } from "./api";

type Answer = Awaited<ReturnType<typeof api.clip>>;

const KEPT = 16;
const answers = new Map<string, Promise<Answer>>(); // by clip and entry, oldest first
const asking = new Map<string, Promise<Answer>>(); // by clip, while its request is under way

export function clipDetails(id: string, entry: unknown): Promise<Answer> {
  const key = `${id}\n${JSON.stringify(entry ?? null)}`;
  // Panes that ask at the same moment share the request, whichever list their copy of the clip came from.
  const kept = answers.get(key) ?? asking.get(id);
  if (kept !== undefined) {
    keep(key, kept);
    return kept;
  }
  const answer = api.clip(id);
  asking.set(id, answer);
  keep(key, answer);
  void answer.then((result) => {
    if (asking.get(id) === answer) asking.delete(id);
    if (!result.ok) for (const [other, value] of answers) if (value === answer) answers.delete(other); // ask again
  });
  return answer;
}

function keep(key: string, answer: Promise<Answer>): void {
  answers.delete(key);
  answers.set(key, answer);
  if (answers.size > KEPT) answers.delete(answers.keys().next().value!);
}

export function forgetClipDetails(): void {
  answers.clear();
  asking.clear();
}
