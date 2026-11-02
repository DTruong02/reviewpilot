import { readFile } from "node:fs";

export function loadConfig(path) {
  return readFile(path);
}

export const normalize = (value) => value.trim();
