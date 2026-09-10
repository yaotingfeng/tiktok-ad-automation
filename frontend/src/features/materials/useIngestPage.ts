import { useState } from "react"

/** Ingest pages default to 100; opaque cursors are never computed by the browser. */
export function useIngestPage() {
  const [cursors, setCursors] = useState<Array<string | null>>([null])
  const [limit, setLimit] = useState(100)
  return {
    cursor: cursors[cursors.length - 1],
    page: cursors.length,
    limit,
    reset: () => setCursors([null]),
    setLimit: (value: number) => {
      setLimit(value === 50 ? 50 : 100)
      setCursors([null])
    },
    next: (cursor: string) => setCursors((previous) => [...previous, cursor]),
    previous: () =>
      setCursors((previous) =>
        previous.length > 1 ? previous.slice(0, -1) : previous,
      ),
  }
}
