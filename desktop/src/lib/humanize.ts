import type { DaemonStatus } from "@/lib/types";

const RUNNING_PHASES = new Set([
  "cycle_started",
  "deciding_batch",
  "deciding_symbol",
  "deciding",
  "executing",
]);

export function processStatusLabel(daemon?: DaemonStatus | null): string {
  if (!daemon?.alive) return "Process not detected";
  const phase = String(daemon.phase ?? "");
  if (RUNNING_PHASES.has(phase) || phase.startsWith("deciding")) return "Cycle active";
  if (phase === "cycle_completed") return "Last cycle complete";
  if (phase === "idle_waiting_for_wake") return "Waiting for next cycle";
  if (phase === "halted") return "Halted";
  if (phase === "ib_connection_failed") return "Market data connection failed";
  if (phase) return "Process detected";
  return "Process detected";
}

export function paperBookLabel(): string {
  return "Paper portfolio";
}

export function marketsLabel(openVenues?: string[] | null): string {
  const open = (openVenues ?? []).filter(Boolean);
  if (!open.length) return "Markets closed";
  return `${open.map(venueLabel).join(" · ")} open`;
}

export function venueLabel(value?: string | null): string {
  const key = String(value ?? "").toUpperCase();
  if (key === "TW") return "Taiwan";
  if (key === "EU") return "Europe";
  if (key === "US") return "United States";
  if (key === "ALL") return "All markets";
  return humanToken(value);
}

export function grossModeLabel(value?: string | null): string {
  const key = String(value ?? "");
  if (key === "risk_off") return "Lower risk appetite";
  if (key === "cautious") return "Selective";
  if (key === "normal") return "Open to opportunity";
  if (key === "watch") return "Watch closely";
  if (key === "selective") return "Selective";
  if (key === "defensive") return "Defensive";
  return sentenceToken(value);
}

export function netBiasLabel(value?: string | null): string {
  const key = String(value ?? "");
  if (key === "short") return "Leaning short";
  if (key === "neutral") return "Balanced";
  if (key === "long") return "Leaning long";
  return sentenceToken(value);
}

export function venuePostureLabel(value?: string | null): string {
  return grossModeLabel(value);
}

export function marketRegimeLabel(value?: string | null): string {
  const key = String(value ?? "").toLowerCase();
  if (key === "risk_off") return "Risk-averse markets";
  if (key === "risk_on") return "Risk-seeking markets";
  if (key === "cautious") return "Cautious markets";
  if (key === "neutral" || key === "normal") return "Balanced markets";
  return sentenceToken(value);
}

export function familyLabel(value?: string | null): string {
  const raw = String(value ?? "").trim().toLowerCase();
  const normalizedKey = raw.replace(/\s+/g, "_");
  const productLabels: Record<string, string> = {
    tw_memory: "Taiwan memory chips",
    tw_pcb: "Taiwan circuit boards",
    tw_semis: "Taiwan semiconductors",
    tw_osat: "Taiwan chip packaging and testing",
    tw_ems_odm: "Taiwan electronics manufacturing",
    eu_consumer_luxury: "European luxury goods",
    eu_luxury_consumer: "European luxury goods",
    eu_industrials_nordic: "Nordic industrial companies",
    eu_materials_nordic: "Nordic materials companies",
    eu_financials_nordic: "Nordic financial companies",
    eu_healthcare_ext: "Extended European healthcare",
    us_consumer_discretionary: "US discretionary consumer businesses",
    us_discretionary_consumer: "US discretionary consumer businesses",
    us_comm: "US communications",
    us_communication: "US communications",
    nasdaq_single_names: "Selected Nasdaq companies",
    defense: "Defense companies",
  };
  if (productLabels[normalizedKey]) return productLabels[normalizedKey];
  const words = String(value ?? "").trim().split(/[_\s]+/).filter(Boolean);
  if (!words.length) return "—";
  const venue = words.shift()?.toLowerCase();
  const venueLabel = venue === "tw" ? "Taiwan" : venue === "eu" ? "European" : venue === "us" ? "US" : venue;
  const expansions: Record<string, string> = {
    disc: "discretionary",
    comm: "communication",
    semis: "semiconductors",
    tech: "technology",
    fin: "financials",
    pcb: "PCB",
    ic: "chip",
  };
  const family = words.map((word) => expansions[word.toLowerCase()] ?? word).join(" ");
  return [venueLabel, family].filter(Boolean).join(" ");
}

export function companyDisplayName(
  companyMap: Record<string, string> | undefined,
  symbol?: string | null,
): string {
  const key = String(symbol ?? "").trim();
  return companyMap?.[key]?.trim() || key || "Portfolio";
}

export function decisionActionLabel(action?: string | null): string {
  const key = String(action ?? "").toUpperCase();
  if (key === "HOLD") return "No change";
  if (key === "BUY") return "Buy";
  if (key === "SELL") return "Sell";
  if (key === "CLOSE") return "Close";
  return sentenceToken(action);
}

export function conditionLabel(value?: string | null): string {
  const text = String(value ?? "").trim();
  const breakout = text.match(/chart_breakout\s*(?:==|>=|<=)\s*(-?1(?:\.0)?)\s*@([0-9]+[mhd])/i);
  if (breakout) {
    const direction = Number(breakout[1]) < 0 ? "Downside" : "Upside";
    return `${direction} breakout on the ${timeframeLabel(breakout[2])} chart`;
  }
  const rangePosition = text.match(/range[_ ]position\s*(?:==|>=|<=|>|<)\s*-?[0-9.]+\s*@([0-9]+[mhd])/i);
  if (rangePosition) {
    return `Recorded price-range threshold on the ${timeframeLabel(rangePosition[1])} chart`;
  }
  const statistical = text.match(/z[_ ]?score\s*(?:==|>=|<=|>|<)\s*-?[0-9.]+\s*@([0-9]+[mhd])/i);
  if (statistical) {
    return `Recorded statistical price threshold on the ${timeframeLabel(statistical[1])} chart`;
  }
  return sentenceToken(text);
}

export function humanToken(value?: string | null): string {
  const text = String(value ?? "").trim();
  if (!text) return "—";
  return text.replaceAll("_", " ");
}

const FIRST_FINANCIAL_FR = {
  screenThesis:
    "La franchise financière est diversifiée et le premier trimestre 2026 est bénéficiaire, avec un équilibre entre revenus d'intérêts et hors intérêts. L'absence de séries comparables sur la croissance et la qualité des actifs laisse toutefois la thèse insuffisamment testée.",
  deepThesis:
    "La qualité opérationnelle paraît encourageante, mais la thèse reste provisoire. Elle doit être confirmée par des données comparables, prudentielles et prospectives plus complètes.",
  business:
    "First Financial Holding est un groupe financier diversifié mais principalement exposé aux activités bancaires.",
} as const;

const MARKET_LANGUAGE_REPLACEMENTS: Array<readonly [RegExp, string]> = [
  [
    /\bsurveillance case rather than a security conclusion\b/gi,
    "company to keep under review rather than a confirmed investment case",
  ],
  [/\b(?:outlook|thesis) is on watch\b/gi, "outlook needs close monitoring"],
  [/\b(?:outlook|thesis) stays on watch\b/gi, "outlook remains under close watch"],
  [/\bcommodity linkage\b/gi, "exposure to commodity prices"],
  [
    /\bautomotive and financing franchise remains profitable and cash[\s_-]*generative\b/gi,
    "automotive and financing businesses remain profitable and generate cash",
  ],
  [/\bautomotive and financing franchise remains\b/gi, "automotive and financing businesses remain"],
  [/\bautomotive and financing franchise\b/gi, "automotive and financing businesses"],
  [/\bsubstantial leverage\b/gi, "high debt"],
  [/\bmodest quarterly operating profitability\b/gi, "modest quarterly operating margins"],
  [/\bwarrant continued monitoring\b/gi, "require continued monitoring"],
  [/\bintact enough for surveillance\b/gi, "intact but still needs monitoring"],
  [/\bprevents a strengthening assessment\b/gi, "does not justify a stronger assessment"],
  [/\bSingapore ATM win\b/gi, "Singapore air-traffic-management contract"],
  [/\bprimary figures\b/gi, "direct financial figures"],
  [
    /\bfactory[\s_-]*fire and valuation headlines are unresolved\b/gi,
    "reports about a factory fire and valuation remain unresolved",
  ],
  [/\boils[\s_-]*energy issuer\b/gi, "oil and gas company"],
  [/\bNeptun Deep installation item\b/gi, "report about the Neptun Deep project"],
  [/\bhybrid[\s_-]*notes\b/gi, "hybrid bond"],
  [/\bguidance sources\b/gi, "company forecast sources"],
  [/appears financially liquid and exposed\b/gi, "has strong liquidity and is exposed"],
  [/appears financially liquid\b/gi, "has strong liquidity"],
  [/\bfinancially liquid\b/gi, "has strong liquidity"],
  [/\bnetwork[\s_-]*infrastructure themes\b/gi, "network infrastructure"],
  [/\bprimary[\s_-]*source strategic disclosures\b/gi, "direct company strategy disclosures"],
  [/\bAPAC\b/g, "Asia-Pacific"],
  [/\bAI[\s_-]*product activity\b/gi, "AI product announcements"],
  [/\baward or recognition noise\b/gi, "awards and recognition without operating evidence"],
  [
    /\bmidnight[\s_-]*implementation (?:copy|reports) still ran in parallel\b/gi,
    "some reports still said the tariffs would begin at midnight",
  ],
  [/\bIran\/(?:ME|Middle East) tensions?\b/gi, "tensions involving Iran and the Middle East"],
  [/\bAsia (?:risk[\s_-]?off|broad risk aversion)\b/gi, "Asian markets moved away from risk"],
  [/Taiwan has no (?:current|live) brief/gi, "Taiwan has no fresh regional update"],
  [
    /Brent near 94 USD and Newmont[’']s gold-linked tape are the situation-backed (?:groups|sleeves)/gi,
    "Brent is near 94 USD and gold-related companies have the clearest support from current conditions",
  ],
  [/Newmont[’']s gold[\s_-]*linked tape/gi, "Newmont and other gold-related companies"],
  [/\bthat venue stays on watch\b/gi, "Taiwan remains under close watch"],
  [/\bTaiwan stays on (?:a )?watch(?:[\s_-]*footing)?\b/gi, "Taiwan remains under close watch"],
  [/\bAsia sold off\b/gi, "Asian markets fell sharply"],
  [/\bGM engine[\s_-]*failure probe\b/gi, "investigation into GM engine failures"],
  [/\bKOSPI rout\b/gi, "sharp fall in South Korea’s KOSPI"],
  [/\boil was still extending gains\b/gi, "oil prices were still rising"],
  [/\bsanctions\/conflict shock\b/gi, "trade and security shock"],
  [/\bME tensions?\b/g, "Middle East tensions"],
  [/\bevidence_items and source_catalog are empty\b/gi, "the current evidence record is empty"],
  [/\bissuer identity is unverified\b/gi, "company identity has not been confirmed"],
  [/\bunverified issuer identity\b/gi, "company identity has not been confirmed"],
  [/\bidentity is unverified\b/gi, "company identity has not been confirmed"],
  [/\bidentity[\s_-]*unverified design paper\b/gi, "a chip-design company whose identity is not confirmed"],
  [/\bidentity[\s_-]*unverified\b/gi, "identity not confirmed"],
  [/\bprovider[\s_-]*standardized reporting period\b/gi, "third-party normalized reporting period"],
  [/\bprovider[\s_-]*standardized expectations\b/gi, "published market forecasts"],
  [/\bprovider failures\b/gi, "data-source failures"],
  [/\bsourced pillar assertions\b/gi, "supported conclusions"],
  [/\bprimary operating proof\b/gi, "direct evidence of business performance"],
  [/\bsecurity conclusion\b/gi, "final conclusion"],
  [/\bcapital[\s_-]*structure event\b/gi, "financing event"],
  [/\bnon[\s_-]*operating income\b/gi, "income outside the core business"],
  [/\bnon[\s_-]*operating volatility\b/gi, "swings outside the core business"],
  [/\bnon[\s_-]*operating items\b/gi, "items outside the core business"],
  [/\bcapital[\s_-]*intensive investment cycle\b/gi, "period of heavy investment"],
  [/\bscreen[\s_-]*depth coverage\b/gi, "a short review of available sources"],
  [/\bat screen[\s_-]*depth\b/gi, "based on a quick review"],
  [/\bscreen[\s_-]*depth\b/gi, "based on a quick review"],
  [/\bprinted a profitable\b/gi, "reported a profitable"],
  [/\bnext earnings print\b/gi, "next earnings report"],
  [/\bMOPS\s+Q1(?:\s+\d{4})?\b/gi, "first quarter in a Taiwan filing"],
  [/\bMOPS\s+Q2(?:\s+\d{4})?\b/gi, "second quarter in a Taiwan filing"],
  [/\bMOPS\s+Q3(?:\s+\d{4})?\b/gi, "third quarter in a Taiwan filing"],
  [/\bMOPS\s+Q4(?:\s+\d{4})?\b/gi, "fourth quarter in a Taiwan filing"],
  [/\bMOPS print\b/gi, "Taiwan filing"],
  [/(\d{4}-\d{2}-\d{2})\s+print\b/gi, "$1 report"],
  [/\bearnings run[\s_-]*rate\b/gi, "sustainable earnings pace"],
  [/\ban earnings compounder\b/gi, "a company with consistently growing earnings"],
  [/\bearnings compounder\b/gi, "company with consistently growing earnings"],
  [/\ba catalyst watch\b/gi, "waiting for a specific event"],
  [/\bcatalyst watch\b/gi, "waiting for a specific event"],
  [/\bcatalyst cases\b/gi, "upcoming events"],
  [/\bforward or management guidance\b/gi, "company forecast"],
  [/\b(?:forward|management) guidance\b/gi, "company forecast"],
  [/\bissuer guidance\b/gi, "a company forecast"],
  [/\bsegment results\b/gi, "business-unit results"],
  [/\bsegment disclosure\b/gi, "business-unit reporting"],
  [/\bsegment economics\b/gi, "business-unit economics"],
  [/\bcomparative periods\b/gi, "prior-period comparisons"],
  [/\bcomparative history\b/gi, "prior-period history"],
  [/\bcomparative performance\b/gi, "prior-period performance"],
  [/\bcompany[\s_-]*quality thesis\b/gi, "company outlook"],
  [/\boperating thesis\b/gi, "company outlook"],
  [/\bfranchise thesis\b/gi, "company outlook"],
  [/\bquality thesis\b/gi, "company outlook"],
  [/\bcompany thesis\b/gi, "company outlook"],
  [/\bthe thesis\b/gi, "the outlook"],
  [/\bthesis\b/gi, "outlook"],
  [/\bon (?:a )?watch[\s_-]*footing\b/gi, "under close watch"],
  [/\bwatch[\s_-]*footing\b/gi, "close watch"],
  [/\bweaker cash[\s_-]*conversion names\b/gi, "companies with weaker cash generation"],
  [/\bcash[\s_-]*conversion names\b/gi, "companies that generate cash"],
  [/\bis cash[\s_-]*generative\b/gi, "generates cash"],
  [/\bcash[\s_-]*generative (\w[\w-]*)/gi, "$1 that generates cash"],
  [/\bcash[\s_-]*generative\b/gi, "generates cash"],
  [/\bcash[\s_-]*conversion\b/gi, "cash generation"],
  [/\ba going[\s_-]*concern\b/gi, "an operating"],
  [/\bgoing[\s_-]*concern\b/gi, "operating"],
  [/\boperating momentum\b/gi, "business momentum"],
  [/\bsurveillance case\b/gi, "company to keep under review"],
  [/\bhardware names\b/gi, "companies"],
  [/\bpackaging names\b/gi, "companies"],
  [/\bpackaging name\b/gi, "company"],
  [/\bcooling names\b/gi, "companies"],
  [/\blocal setups\b/gi, "local prospects"],
  [/\bcleaner cash\b/gi, "stronger cash generation"],
  [/\blong watch\b/gi, "potential Long position under review"],
  [/\bdeterministic fallback\b/gi, "default company list"],
  [/\bdeterministic list\b/gi, "broader comparison"],
  [/\b(?:broader )?rules[\s_-]*based list\b/gi, "default company list"],
  [/\brisk assets\b/gi, "riskier investments"],
  [/\bglobal risk[\s_-]?off mood\b/gi, "global move away from risk"],
  [/\brisk[\s_-]?off\b/gi, "broad risk aversion"],
  [/\bglobal broad risk aversion mood\b/gi, "global move away from risk"],
  [/\bAsia broad risk aversion\b/gi, "Asian markets moved away from risk"],
  [/\brisk[\s_-]?on\b/gi, "renewed risk appetite"],
  [/\bhydrocarbon names\b/gi, "oil and gas companies"],
  [/\bhydrocarbons\b/gi, "oil and gas companies"],
  [/\boil-linked names\b/gi, "oil and gas companies"],
  [/\bgold-linked materials\b/gi, "gold-related materials companies"],
  [/\bgold-linked tape\b/gi, "gold-related companies"],
  [/\bdiplomacy-versus-escalation tape\b/gi, "tension between diplomacy and escalation"],
  [/\bthe tape\b/gi, "the market"],
  [/\bthe other tape\b/gi, "another supported group"],
  [/\bsleeves\b/gi, "groups"],
  [/\bagent picks\b/gi, "previous selections"],
  [/\bcandidate books\b/gi, "candidate lists"],
  [/\bradar bias\b/gi, "research emphasis"],
  [/\bthe book\b/gi, "the portfolio"],
  [/\bboard tape\b/gi, "circuit-board market"],
  [/\bAI-market tape\b/gi, "AI-market evidence"],
  [/\bcut the long\b/gi, "reduce confidence in a long position"],
  [/\bselective frame\b/gi, "selective approach"],
  [/\bsituation-backed groups\b/gi, "companies supported by current conditions"],
  [/\brather than being pressed\b/gi, "without increasing exposure"],
  [/\bno net directional lean\b/gi, "no strong portfolio direction"],
  [/^quiet_gate$/gi, "No material change required another review"],
  [/\blive comparative opportunity\b/gi, "clearest relative opportunity"],
  [/\blive\b(?!-)/gi, "current"],
  [/\bcopy\b/gi, "reports"],
  [/\boil bid intact\b/gi, "Oil prices remain supported"],
  [/\bPHP\b/g, "the Philippine peso"],
  [/\bin the overlay\b/gi, "in the broader context"],
  [/\bUSD range-bound\b/gi, "The US dollar is trading in a narrow range"],
  [/\bdovish Fed priced\b/gi, "easier Federal Reserve policy expected"],
  [/\bslide-on-yields reports\b/gi, "reports of a fall linked to interest rates"],
  [/\bFed funds\b/gi, "US central bank rate"],
  [/\bFOMC\b/g, "Federal Reserve meeting"],
  [/\bOCI\b/g, "other accounting items"],
  [/\bpackaging[\s_-]*test[\s_-]*EMS platform\b/gi, "semiconductor packaging, testing and manufacturing company"],
  [/\bAI[\s_-]*(?:LEAP\/ATM|LEAP|ATM) demand\b/gi, "AI-related demand"],
  [/\bgrowth pillar\b/gi, "growth claim"],
  [/\bprimary transcript\b/gi, "direct company transcript"],
  [/\bH1\b/g, "first half"],
  [/\bH2\b/g, "second half"],
  [/\bQ1\b/g, "first quarter"],
  [/\bQ2\b/g, "second quarter"],
  [/\bQ3\b/g, "third quarter"],
  [/\bQ4\b/g, "fourth quarter"],
  [/\bFY(\d{4})\b/gi, "full-year $1"],
  [/\bFY\b/g, "fiscal year"],
  [/\bfranchise\b/gi, "business"],
];

export function plainMarketLanguage(value?: string | null): string {
  let text = String(value ?? "")
    .replaceAll(
      FIRST_FINANCIAL_FR.screenThesis,
      "The financial business is diversified and the first quarter of 2026 is profitable, with a balance between interest and non-interest income. The lack of comparable figures on growth and asset quality still leaves the outlook insufficiently tested.",
    )
    .replaceAll(
      FIRST_FINANCIAL_FR.deepThesis,
      "Operating quality looks encouraging, but the outlook remains provisional. It still needs confirmation from more complete comparable, prudential and forward-looking data.",
    )
    .replaceAll(
      FIRST_FINANCIAL_FR.business,
      "First Financial Holding is a diversified financial group but mainly exposed to banking.",
    );
  for (const [pattern, replacement] of MARKET_LANGUAGE_REPLACEMENTS) {
    text = text.replace(pattern, replacement);
  }
  return capitalizeSentenceStarts(text);
}

function capitalizeSentenceStarts(text: string): string {
  return text.replace(/(^|[.!?]\s+)([a-z])/g, (match, prefix: string, letter: string, offset: number) => {
    if (offset > 0 && /[A-Z]\.$/.test(text.slice(Math.max(0, offset - 2), offset + 1))) return match;
    return `${prefix}${letter.toUpperCase()}`;
  });
}

function sentenceToken(value?: string | null): string {
  const text = humanToken(value);
  if (text === "—") return text;
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
}

function timeframeLabel(value: string): string {
  const match = value.match(/^(\d+)([mhd])$/i);
  if (!match) return value;
  const unit = match[2].toLowerCase() === "m" ? "minute" : match[2].toLowerCase() === "h" ? "hour" : "day";
  return `${match[1]}-${unit}`;
}

export function projectionFreshness(input?: {
  status?: string | null;
  as_of?: string | null;
  valid_until?: string | null;
} | null): { current: boolean; label: string } {
  if (!input) return { current: false, label: "Update unavailable" };
  const until = input.valid_until ? Date.parse(input.valid_until) : Number.NaN;
  const expired = Number.isFinite(until) && until < Date.now();
  const status = String(input.status ?? "").toLowerCase();
  if (expired) return { current: false, label: "Needs refreshing" };
  if (["partial", "incomplete", "degraded"].includes(status)) {
    return { current: false, label: status === "partial" ? "Partial coverage" : sentenceToken(status) };
  }
  const statusBlocks =
    Boolean(status) && ["stale", "expired", "error", "failed", "invalid"].includes(status);
  const current = Boolean(input.as_of || status) && !expired && !statusBlocks;
  if (statusBlocks) return { current: false, label: humanToken(status) };
  if (current && Number.isFinite(until)) return { current: true, label: "Current" };
  if (current && ["current", "fresh", "success"].includes(status)) {
    return { current: true, label: "Current" };
  }
  if (current) return { current: false, label: "Freshness not guaranteed" };
  return { current: false, label: "Update time unavailable" };
}
