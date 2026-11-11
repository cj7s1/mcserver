/** The runtime_images setting (GameImagesSection.tsx). */
/** The setting value from the form texts: games with an empty field are left out (their default applies). */
export function runtimeImagesValue(texts: Record<string, string>): Record<string, { repository: string }> {
  const out: Record<string, { repository: string }> = {};
  for (const [game, repo] of Object.entries(texts)) if (repo.trim()) out[game] = { repository: repo.trim() };
  return out;
}
