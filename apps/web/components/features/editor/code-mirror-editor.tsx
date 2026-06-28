"use client";

import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import type { Extension } from "@codemirror/state";
import { EditorState } from "@codemirror/state";
import { EditorView, highlightActiveLine, keymap, lineNumbers } from "@codemirror/view";
import { useEffect, useRef } from "react";
import { yukarExtensions } from "@/lib/codemirror/yukar-theme";

interface CodeMirrorEditorProps {
  value: string;
  onChange?: (value: string) => void;
  language?: "yaml" | "markdown" | "text";
  readonly?: boolean;
}

export function CodeMirrorEditor({
  value,
  onChange,
  language = "text",
  readonly = false,
}: CodeMirrorEditorProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  // Capture initial value for editor creation only — value sync is handled by the second useEffect
  const initialValueRef = useRef(value);

  // Editor is only re-mounted when language/readonly changes; value sync uses the second useEffect
  useEffect(() => {
    if (!containerRef.current) return;

    let cancelled = false;

    async function createEditor() {
      const extensions: Extension[] = [
        ...yukarExtensions,
        lineNumbers(),
        highlightActiveLine(),
        history(),
        keymap.of([...defaultKeymap, ...historyKeymap]),
        EditorView.updateListener.of((update) => {
          if (update.docChanged && onChangeRef.current) {
            onChangeRef.current(update.state.doc.toString());
          }
        }),
      ];

      if (readonly) {
        extensions.push(EditorState.readOnly.of(true));
      }

      // Lazy-load language extensions (CodeMirror is client-only)
      if (language === "yaml") {
        const { yaml } = await import("@codemirror/lang-yaml");
        extensions.push(yaml());
      } else if (language === "markdown") {
        const { markdown } = await import("@codemirror/lang-markdown");
        extensions.push(markdown());
      }

      // Check for cancellation or container removal after the dynamic import
      if (cancelled || !containerRef.current) return;

      const state = EditorState.create({
        doc: initialValueRef.current,
        extensions,
      });

      if (viewRef.current) {
        viewRef.current.destroy();
      }

      viewRef.current = new EditorView({
        // biome-ignore lint/style/noNonNullAssertion: containerRef checked above; guaranteed non-null at this point
        parent: containerRef.current!,
        state,
      });
    }

    createEditor();

    return () => {
      cancelled = true;
      viewRef.current?.destroy();
      viewRef.current = null;
    };
  }, [language, readonly]);

  // Sync external value changes (e.g. tab switch)
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    if (current !== value) {
      view.dispatch({
        changes: { from: 0, to: current.length, insert: value },
      });
    }
  }, [value]);

  return (
    <div
      ref={containerRef}
      className="h-full w-full overflow-auto [&_.cm-editor]:h-full [&_.cm-editor]:outline-none [&_.cm-scroller]:h-full [&_.cm-scroller]:font-mono"
    />
  );
}
