import React from "react";

export interface Props {
  label: string;
}

export function Label(props: Props) {
  return <span>{props.label}</span>;
}
