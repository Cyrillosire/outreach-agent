"use client";

import { useState } from "react";

import { inputClass } from "@/components/ui";
import { linesToList } from "@/lib/lists";

/**
 * Textarea for a list of short strings, one per line. Keeps the raw text while typing so blank lines
 * aren't swallowed mid-edit; reports the cleaned list on every change.
 */
export function ListInput({ id, value, onChange, rows = 3, placeholder }: {
  id: string;
  value: string[];
  onChange: (items: string[]) => void;
  rows?: number;
  placeholder?: string;
}) {
  const [text, setText] = useState(value.join("\n"));
  return (
    <textarea
      id={id} rows={rows} placeholder={placeholder} className={inputClass} value={text}
      onChange={(e) => { setText(e.target.value); onChange(linesToList(e.target.value)); }}
    />
  );
}
