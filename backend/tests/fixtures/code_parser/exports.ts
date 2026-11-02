export function parse(value: string): string;
export function parse(value: number): number;
export function parse(value: string | number): string | number {
  return value;
}

function localHelper() {
  return true;
}

export { localHelper as publicHelper };

function outer() {
  function localHelper() {
    return false;
  }
  return localHelper();
}
