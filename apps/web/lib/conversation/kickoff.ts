/**
 * lib/conversation/kickoff.ts
 *
 * Parses the host-generated turn-0 prompts (Manager epic kickoff, Worker /
 * Evaluator task hand-off, Reviewer seed) into a structured view so the
 * conversation can render the human-relevant parts (title, description,
 * acceptance criteria, contract) and fold the instruction boilerplate.
 *
 * The formats are produced by apps/api/src/yukar/agents/prompts.py
 * (_build_manager_prompt / _build_worker_prompt / _build_evaluator_prompt /
 * _build_reviewer_prompt). Parsing is defensive: any text that does not match
 * returns null and the message renders as-is.
 */

export interface KickoffSection {
  /** Section label shown above the text (e.g. "Acceptance Criteria"); null = lead description. */
  label: string | null;
  text: string;
}

export interface KickoffView {
  kind: "epic" | "task";
  title: string;
  sections: KickoffSection[];
}

/**
 * Host boilerplate sentences appended INSIDE user-authored sections
 * (prompts.py joins them into the same block). Anything from the first
 * matching marker onward is folded away from the visible section.
 */
const SECTION_BOILERPLATE_MARKERS = [
  "\n\nThese criteria define the overall done-conditions",
  "\n\nThese are the objective done-conditions",
  "\n\nImplement exactly what the contract specifies",
  "\n\nEvaluate ONLY against the contract",
];

function stripSectionBoilerplate(body: string): string {
  let cut = body.length;
  for (const marker of SECTION_BOILERPLATE_MARKERS) {
    const i = body.indexOf(marker);
    if (i !== -1 && i < cut) cut = i;
  }
  return body.slice(0, cut).trim();
}

/** Split markdown text into `# `-heading sections (leading text = heading null). */
function splitHeadingSections(text: string): Array<{ heading: string | null; body: string }> {
  const lines = text.split("\n");
  const sections: Array<{ heading: string | null; body: string }> = [];
  let heading: string | null = null;
  let buf: string[] = [];
  const flush = () => {
    if (heading !== null || buf.join("").trim()) {
      sections.push({ heading, body: buf.join("\n").trim() });
    }
    buf = [];
  };
  for (const line of lines) {
    if (line.startsWith("# ")) {
      flush();
      heading = line.slice(2).trim();
    } else {
      buf.push(line);
    }
  }
  flush();
  return sections;
}

/**
 * Parse a turn-0 kickoff prompt. Returns null when the text is not a known
 * kickoff format (the caller falls back to plain rendering).
 */
export function parseKickoff(text: string): KickoffView | null {
  const sections = splitHeadingSections(text);
  const first = sections[0];
  if (!first?.heading) return null;

  const epicMatch = /^Epic(?: under review)?: (.+)$/.exec(first.heading);
  if (epicMatch) {
    const view: KickoffView = {
      kind: "epic",
      title: epicMatch[1],
      sections: [],
    };
    if (first.body) view.sections.push({ label: null, text: first.body });
    for (const s of sections.slice(1)) {
      if (s.heading === "Acceptance Criteria") {
        const ac = stripSectionBoilerplate(s.body);
        if (ac) view.sections.push({ label: s.heading, text: ac });
      }
      // Docs / instruction sections stay folded (available via "show full prompt").
    }
    return view;
  }

  const taskMatch = /^Task: (.+)$/.exec(first.heading);
  if (taskMatch) {
    const view: KickoffView = {
      kind: "task",
      title: taskMatch[1],
      sections: [],
    };
    for (const s of sections.slice(1)) {
      if (s.heading?.startsWith("Task Contract")) {
        const contract = stripSectionBoilerplate(s.body);
        if (contract) view.sections.push({ label: "Contract", text: contract });
      }
      // The working-directory line (in the lead body) and instructions stay folded.
    }
    return view;
  }

  return null;
}
