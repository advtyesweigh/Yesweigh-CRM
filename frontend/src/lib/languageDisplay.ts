const ISO_TO_NAME: Record<string, string> = {
  ml: "Malayalam",
  en: "English",
  hi: "Hindi",
  ta: "Tamil",
  te: "Telugu",
  kn: "Kannada",
  mr: "Marathi",
  bn: "Bengali",
  gu: "Gujarati",
  pa: "Punjabi",
  or: "Odia",
  ur: "Urdu",
  as: "Assamese",
};

const NAME_TO_LABEL: Record<string, string> = {
  malayalam: "Malayalam",
  english: "English",
  hindi: "Hindi",
  tamil: "Tamil",
  telugu: "Telugu",
  kannada: "Kannada",
  marathi: "Marathi",
  bengali: "Bengali",
  gujarati: "Gujarati",
  punjabi: "Punjabi",
  odia: "Odia",
  oriya: "Odia",
  urdu: "Urdu",
  assamese: "Assamese",
  മലയാളം: "Malayalam",
};

/** Map ISO codes or free-text contact language to a display label. */
export function formatLanguageLabel(raw?: string | null): string {
  const s = (raw || "").trim();
  if (!s) return "";
  const primary = s.split(",")[0].trim();
  const lower = primary.toLowerCase();
  if (NAME_TO_LABEL[lower]) return NAME_TO_LABEL[lower];
  const base = lower.replace(/_/g, "-").split("-")[0];
  if (ISO_TO_NAME[base]) return ISO_TO_NAME[base];
  if (/^[a-z]{2}(-[a-z]{2})?$/i.test(primary)) return primary.toUpperCase();
  return primary;
}