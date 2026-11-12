/** Content search across text files (POST /instances/{id}/files/search). */
import { useMutation } from "@tanstack/react-query";
import { FileText, Loader2, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { CutOffNotice } from "@/components/CutOffNotice";
import { EmptyState, ErrorState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { api } from "@/lib/api";
import { SEARCHED_EXTENSIONS } from "./file-utils";

interface SearchHit {
  path: string;
  line: number | null;
  text: string | null;
}

const MAX_RESULTS = 200;
const SEARCH_CUT_OFF = "Search stopped early: the node looks at a limited number of files (or for a limited time), so not every match is listed. Search a smaller folder to see the rest.";

function Highlight({ text, query }: { text: string; query: string }) {
  const i = text.toLowerCase().indexOf(query.toLowerCase());
  if (i < 0 || !query) return <>{text}</>;
  return (
    <>
      {text.slice(0, i)}
      <mark className="rounded-sm bg-amber-300/60 px-0.5 text-foreground dark:bg-amber-400/30">{text.slice(i, i + query.length)}</mark>
      {text.slice(i + query.length)}
    </>
  );
}

export function FileSearchSheet({
  instanceId,
  scope,
  open,
  onOpenChange,
  onOpenResult,
}: {
  instanceId: string;
  scope: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onOpenResult: (path: string, line: number | null) => void;
}) {
  const [query, setQuery] = useState("");
  const [wholeServer, setWholeServer] = useState(false);
  const search = useMutation({
    mutationFn: (vars: { query: string; path: string }) =>
      api
        .post<{ results: SearchHit[]; truncated?: boolean }>(`/instances/${instanceId}/files/search`, vars)
        // truncated: the node stopped early (its cap on files looked at, or time; B-6)
        .then((r) => ({ ...vars, results: r.results, truncated: !!r.truncated })),
  });
  const effectiveScope = wholeServer ? "" : scope;
  const data = search.data;

  const grouped = useMemo(() => {
    const map = new Map<string, SearchHit[]>();
    for (const hit of data?.results ?? []) {
      const list = map.get(hit.path) ?? [];
      list.push(hit);
      map.set(hit.path, list);
    }
    return [...map.entries()];
  }, [data]);

  const submit = () => {
    const q = query.trim();
    if (q.length < 2 || search.isPending) return;
    search.mutate({ query: q, path: effectiveScope });
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full gap-0 sm:max-w-lg">
        <SheetHeader className="border-b pr-12">
          <SheetTitle>Search in files</SheetTitle>
          <SheetDescription>
            Case-insensitive text search in config and log files (up to 2 MB each, first {MAX_RESULTS} matches).
          </SheetDescription>
        </SheetHeader>
        <form
          className="grid gap-3 border-b p-4"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <div className="flex gap-2">
            <Label htmlFor="file-search-query" className="sr-only">
              Search text
            </Label>
            <Input
              id="file-search-query"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="e.g. view-distance"
              autoFocus
              autoComplete="off"
              spellCheck={false}
              maxLength={200}
            />
            <Button type="submit" disabled={query.trim().length < 2 || search.isPending}>
              {search.isPending ? <Loader2 className="animate-spin" /> : <Search />} Search
            </Button>
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            {scope ? (
              <>
                <Checkbox id="file-search-all" checked={wholeServer} onCheckedChange={(v) => setWholeServer(v === true)} />
                <Label htmlFor="file-search-all" className="text-xs font-normal">
                  Search the whole server instead of <span className="font-mono">/{scope}</span>
                </Label>
              </>
            ) : (
              <span>Searching the whole server folder.</span>
            )}
          </div>
          <p className="text-[11px] text-muted-foreground">File types: {SEARCHED_EXTENSIONS.join(" ")}</p>
        </form>
        <div className="min-h-0 flex-1 overflow-y-auto p-4" aria-live="polite">
          {search.isPending ? (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" /> Searching… large servers can take a while.
            </p>
          ) : search.error ? (
            <ErrorState error={search.error} title="Search failed" onRetry={submit} />
          ) : !data ? (
            <EmptyState icon={<Search className="size-5" />} title="Find text across files" description="Type at least 2 characters." />
          ) : data.results.length === 0 ? (
            <div className="grid gap-3">
              {data.truncated && <CutOffNotice>{SEARCH_CUT_OFF}</CutOffNotice>}
              <EmptyState
                title="No matches"
                description={
                  <>
                    Nothing matched <span className="font-mono">{data.query}</span> in{" "}
                    <span className="font-mono">/{data.path}</span>
                    {data.truncated ? " in the files searched so far" : ""}.
                  </>
                }
              />
            </div>
          ) : (
            <div className="grid gap-3">
              {data.truncated && <CutOffNotice>{SEARCH_CUT_OFF}</CutOffNotice>}
              <p className="text-xs text-muted-foreground">
                {data.results.length >= MAX_RESULTS
                  ? `Showing the first ${MAX_RESULTS} matches — narrow the search to see more.`
                  : `${data.results.length} match${data.results.length === 1 ? "" : "es"} in ${grouped.length} file${grouped.length === 1 ? "" : "s"}.`}
              </p>
              {grouped.map(([path, hits]) => (
                <div key={path} className="overflow-hidden rounded-lg border">
                  <button
                    type="button"
                    onClick={() => onOpenResult(path, hits.find((h) => h.line)?.line ?? null)}
                    className="flex w-full items-center gap-2 bg-muted/40 px-3 py-1.5 text-left font-mono text-xs hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none focus-visible:ring-inset"
                  >
                    <FileText className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
                    <span className="truncate">
                      <Highlight text={path} query={data.query} />
                    </span>
                  </button>
                  {hits.some((h) => h.line) && (
                    <ul className="divide-y">
                      {hits
                        .filter((h) => h.line)
                        .map((h) => (
                          <li key={`${h.line}`}>
                            <button
                              type="button"
                              onClick={() => onOpenResult(path, h.line)}
                              className="flex w-full gap-3 px-3 py-1 text-left font-mono text-xs hover:bg-muted/60 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none focus-visible:ring-inset"
                              aria-label={`Open ${path} at line ${h.line}`}
                            >
                              <span className="w-10 shrink-0 text-right text-muted-foreground tabular-nums">{h.line}</span>
                              <span className="min-w-0 truncate">
                                <Highlight text={h.text ?? ""} query={data.query} />
                              </span>
                            </button>
                          </li>
                        ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
