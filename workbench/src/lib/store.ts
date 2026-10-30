import { useEffect, useState } from "react";

export function useLocalStorage<T>(key: string, initial: T) {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
  }, [key, value]);
  return [value, setValue] as const;
}

export type Stock = { code: string; name: string; note: string; addedAt: string };
export type Trade = {
  id: string; code: string; name: string; side: "buy" | "sell";
  price: number; qty: number; date: string; note: string;
};
export type Note = { id: string; date: string; text: string };
export type Todo = { id: string; text: string; done: boolean };
export type Link = { id: string; title: string; url: string };

export type WorkbenchData = {
  stocks: Stock[]; trades: Trade[]; notes: Note[];
  todos: Todo[]; links: Link[];
  exportAll: () => void;
  importAll: (json: string) => boolean;
};
