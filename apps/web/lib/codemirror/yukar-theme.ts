import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { EditorView } from "@codemirror/view";
import { tags as t } from "@lezer/highlight";

/**
 * yukar CodeMirror theme (conforms to DESIGN.md tokens)
 * Fixed monochrome dark: white primary + cyan secondary-container
 */
export const yukarTheme = EditorView.theme(
  {
    "&": {
      color: "#e5e1e4",
      backgroundColor: "#131315",
      height: "100%",
    },
    ".cm-content": {
      caretColor: "#00e3fd",
      fontFamily: "var(--font-mono)",
      fontSize: "14px",
      lineHeight: "22px",
      padding: "8px 0",
    },
    ".cm-cursor, .cm-dropCursor": {
      borderLeftColor: "#00e3fd",
    },
    "&.cm-focused .cm-selectionBackground, .cm-selectionBackground": {
      backgroundColor: "rgba(0, 227, 253, 0.15)",
    },
    ".cm-panels": {
      backgroundColor: "#1c1b1d",
      color: "#e5e1e4",
    },
    ".cm-panels.cm-panels-top": {
      borderBottom: "1px solid #444748",
    },
    ".cm-panels.cm-panels-bottom": {
      borderTop: "1px solid #444748",
    },
    ".cm-searchMatch": {
      backgroundColor: "rgba(0, 227, 253, 0.2)",
      outline: "1px solid rgba(0, 227, 253, 0.4)",
    },
    ".cm-searchMatch.cm-searchMatch-selected": {
      backgroundColor: "rgba(0, 227, 253, 0.35)",
    },
    ".cm-activeLine": {
      backgroundColor: "rgba(255, 255, 255, 0.03)",
    },
    ".cm-selectionMatch": {
      backgroundColor: "rgba(255, 255, 255, 0.08)",
    },
    "&.cm-focused .cm-matchingBracket, &.cm-focused .cm-nonmatchingBracket": {
      backgroundColor: "rgba(255, 255, 255, 0.1)",
    },
    ".cm-gutters": {
      backgroundColor: "#131315",
      color: "#444748",
      border: "none",
      borderRight: "1px solid #201f22",
    },
    ".cm-activeLineGutter": {
      backgroundColor: "rgba(255, 255, 255, 0.03)",
      color: "#8e9192",
    },
    ".cm-foldPlaceholder": {
      backgroundColor: "transparent",
      border: "none",
      color: "#8e9192",
    },
    ".cm-tooltip": {
      border: "1px solid #444748",
      backgroundColor: "#201f22",
      color: "#e5e1e4",
    },
    ".cm-tooltip .cm-tooltip-arrow:before": {
      borderTopColor: "#444748",
    },
    ".cm-tooltip .cm-tooltip-arrow:after": {
      borderTopColor: "#201f22",
    },
    ".cm-tooltip.cm-completionInfo": {
      padding: "8px 12px",
    },
  },
  { dark: true },
);

export const yukarHighlightStyle = HighlightStyle.define([
  { tag: t.keyword, color: "#bdf4ff" },
  { tag: [t.name, t.deleted, t.character, t.propertyName, t.macroName], color: "#e5e1e4" },
  { tag: [t.function(t.variableName), t.labelName], color: "#ffffff" },
  { tag: [t.color, t.constant(t.name), t.standard(t.name)], color: "#bdf4ff" },
  { tag: [t.definition(t.name), t.separator], color: "#e5e1e4" },
  {
    tag: [
      t.typeName,
      t.className,
      t.number,
      t.changed,
      t.annotation,
      t.modifier,
      t.self,
      t.namespace,
    ],
    color: "#c4c7c8",
  },
  {
    tag: [t.operator, t.operatorKeyword, t.url, t.escape, t.regexp, t.link, t.special(t.string)],
    color: "#00e3fd",
  },
  { tag: [t.meta, t.comment], color: "#8e9192" },
  { tag: t.strong, fontWeight: "bold" },
  { tag: t.emphasis, fontStyle: "italic" },
  { tag: t.strikethrough, textDecoration: "line-through" },
  { tag: t.link, color: "#00e3fd", textDecoration: "underline" },
  { tag: t.heading, fontWeight: "bold", color: "#ffffff" },
  { tag: [t.atom, t.bool, t.special(t.variableName)], color: "#bdf4ff" },
  { tag: [t.processingInstruction, t.string, t.inserted], color: "#9cf0ff" },
  { tag: t.invalid, color: "#ffb4ab" },
]);

export const yukarExtensions = [yukarTheme, syntaxHighlighting(yukarHighlightStyle)];
