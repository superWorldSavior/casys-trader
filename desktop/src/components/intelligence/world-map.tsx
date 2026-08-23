/**
 * WorldMap — Choroplèthe géopolitique + marqueurs venues.
 *
 * Props:
 *   countryCounts  — nombre d'événements géopolitiques récents par nom de pays
 *                    (vocabulaire GDELT : "United States", "Russia"…)
 *   venues         — posture par venue (TW / EU / US)
 */
import { useMemo } from "react";
import { geoNaturalEarth1, geoPath } from "d3-geo";
import { feature } from "topojson-client";
import type { Topology } from "topojson-specification";
import type { FeatureCollection } from "geojson";
import { venueLabel, venuePostureLabel } from "@/lib/humanize";
// resolveJsonModule: true — Vite + TS bundler resolution gèrent l'import JSON
// NOTE world-atlas 110m : résolution ~110 km — les micro-États (Singapore, Monaco,
// Luxembourg, Liechtenstein…) sont absents du fond de carte ou fusionnés avec leurs
// voisins. Upgrader vers countries-50m pour une meilleure couverture si nécessaire.
import topo from "world-atlas/countries-110m.json";

// ── Normalisation des noms ────────────────────────────────────────────────────
// GDELT / noms courants  →  world-atlas properties.name
const NAME_ALIASES: Record<string, string> = {
  "United States": "United States of America",
  "United States of America": "United States of America",
  "Czech Republic": "Czechia",
  "Republic of Korea": "South Korea",
  "Korea, Republic of": "South Korea",
  "Korea, South": "South Korea",
  "Democratic Republic of Congo": "Dem. Rep. Congo",
  "Democratic Republic of the Congo": "Dem. Rep. Congo",
  "DR Congo": "Dem. Rep. Congo",
  "Republic of Congo": "Congo",
  "Bosnia and Herzegovina": "Bosnia and Herz.",
  "Bosnia & Herzegovina": "Bosnia and Herz.",
  "North Macedonia": "Macedonia",
  "Ivory Coast": "Côte d'Ivoire",
  "Cote d'Ivoire": "Côte d'Ivoire",
  "Eswatini": "eSwatini",
  "Swaziland": "eSwatini",
  "East Timor": "Timor-Leste",
  "Brunei Darussalam": "Brunei",
  "Syrian Arab Republic": "Syria",
  "Lao PDR": "Laos",
  "Lao People's Democratic Republic": "Laos",
  "Viet Nam": "Vietnam",
  "Russian Federation": "Russia",
  "Iran, Islamic Republic of": "Iran",
  "Korea, Democratic People's Republic of": "North Korea",
  "Moldova, Republic of": "Moldova",
  "Tanzania, United Republic of": "Tanzania",
  "Palestinian Territory": "Palestine",
  "Occupied Palestinian Territory": "Palestine",
  "Palestine, State of": "Palestine",
  "United Arab Emirates": "United Arab Emirates",
};

function normalizeName(name: string): string {
  return NAME_ALIASES[name] ?? name;
}

// ── Coordonnées approx. des venues (lon, lat) ─────────────────────────────────
const VENUE_COORDS: Record<string, [number, number]> = {
  US: [-98, 39],
  EU: [10, 50],
  TW: [121, 23.8],
};

// ── Couleur selon posture venue ───────────────────────────────────────────────
function postureColor(posture: string | null): string {
  if (!posture) return "var(--color-accent)";
  const p = posture.toLowerCase();
  if (p.includes("favor")) return "var(--color-gain)";
  if (p.includes("select")) return "var(--color-warn)";
  if (p.includes("watch") || p.includes("cautious") || p.includes("reduce")) return "var(--color-faint)";
  return "var(--color-accent)";
}

// ── Palier d'activité ─────────────────────────────────────────────────────────
type FillSpec = { fill: string; fillOpacity: number };

function activityFill(count: number): FillSpec {
  if (count === 0) return { fill: "var(--color-meter)", fillOpacity: 1 };
  if (count === 1) return { fill: "var(--color-accent)", fillOpacity: 0.22 };
  if (count <= 3) return { fill: "var(--color-accent)", fillOpacity: 0.45 };
  return { fill: "var(--color-accent)", fillOpacity: 0.75 };
}

// ── Types internes ────────────────────────────────────────────────────────────
type PathItem = FillSpec & {
  key: string;
  d: string;
  count: number;
  name: string;
};

type MarkerItem = {
  venue: string;
  posture: string | null;
  x: number;
  y: number;
};

// ── Composant ─────────────────────────────────────────────────────────────────
export type WorldMapProps = {
  countryCounts: Record<string, number>;
  venues: Record<string, string | null>;
};

export function WorldMap({ countryCounts, venues }: WorldMapProps) {
  const { paths, markers } = useMemo(() => {
    // Projection Natural Earth — 960×500 standard
    const projection = geoNaturalEarth1().scale(153).translate([480, 250]);
    const pathGen = geoPath(projection);

    // Conversion TopoJSON → GeoJSON FeatureCollection
    const countries = feature(
      topo as unknown as Topology,
      "countries",
    ) as unknown as FeatureCollection;

    // Index des counts normalisés
    const counts: Record<string, number> = {};
    for (const [name, n] of Object.entries(countryCounts)) {
      const key = normalizeName(name);
      counts[key] = (counts[key] ?? 0) + n;
    }

    const paths: PathItem[] = [];
    for (let i = 0; i < countries.features.length; i++) {
      const f = countries.features[i];
      const props = f.properties as Record<string, string> | null;
      const name = props?.["name"] ?? "";
      const count = counts[name] ?? 0;
      const d = pathGen(f);
      if (!d) continue;
      paths.push({
        key: String(f.id ?? i),
        d,
        count,
        name,
        ...activityFill(count),
      });
    }

    const markers: MarkerItem[] = [];
    for (const [venue, posture] of Object.entries(venues)) {
      const coords = VENUE_COORDS[venue];
      if (!coords) continue;
      const pt = projection(coords);
      if (!pt) continue;
      markers.push({ venue, posture, x: pt[0], y: pt[1] });
    }

    return { paths, markers };
  }, [countryCounts, venues]);

  return (
    <div>
      {/* Carte SVG responsive — aspect ratio 960:500 garanti par viewBox */}
      <svg
        viewBox="0 0 960 500"
        style={{ width: "100%", height: "auto", display: "block" }}
        aria-label="World geopolitical activity map"
        role="img"
      >
        {/* Fond de carte */}
        <rect width={960} height={500} fill="var(--color-ink)" />

        {/* Choroplèthe pays */}
        {paths.map((p) => (
          <path
            key={p.key}
            d={p.d}
            fill={p.fill}
            fillOpacity={p.fillOpacity}
            stroke="var(--color-hairline)"
            strokeWidth={0.4}
            strokeLinejoin="round"
          >
            <title>
              {p.name}
              {p.count > 0
                ? ` — ${p.count} event${p.count === 1 ? "" : "s"} in the past 3 days`
                : ""}
            </title>
          </path>
        ))}

        {/* Marqueurs venues */}
        {markers.map((m) => (
          <g key={m.venue}>
            {/* Halo */}
            <circle
              cx={m.x}
              cy={m.y}
              r={9}
              fill={postureColor(m.posture)}
              fillOpacity={0.15}
            />
            {/* Point */}
            <circle
              cx={m.x}
              cy={m.y}
              r={5}
              fill={postureColor(m.posture)}
              stroke="var(--color-panel)"
              strokeWidth={1.5}
            />
            {/* Label */}
            <text
              x={m.x + 11}
              y={m.y + 4}
              fontSize={9}
              fontFamily="IBM Plex Mono, ui-monospace, monospace"
              fill="var(--color-fg)"
              style={{ pointerEvents: "none", userSelect: "none" }}
            >
              {venueLabel(m.venue)} · {venuePostureLabel(m.posture)}
            </text>
          </g>
        ))}
      </svg>

      {/* Légende compacte */}
      <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 px-1">
        <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
          Activity
        </span>
        <LegendTier
          fill="var(--color-meter)"
          fillOpacity={1}
          shape="square"
          label="none"
        />
        <LegendTier
          fill="var(--color-accent)"
          fillOpacity={0.22}
          shape="square"
          label="1 event"
        />
        <LegendTier
          fill="var(--color-accent)"
          fillOpacity={0.45}
          shape="square"
          label="2–3"
        />
        <LegendTier
          fill="var(--color-accent)"
          fillOpacity={0.75}
          shape="square"
          label="4+"
        />

        <span className="ml-1 font-mono text-[9px] uppercase tracking-[0.14em] text-faint">
          Regions
        </span>
        <LegendTier fill="var(--color-gain)" shape="dot" label="Preferred" />
        <LegendTier fill="var(--color-warn)" shape="dot" label="Selective" />
        <LegendTier fill="var(--color-faint)" shape="dot" label="Watch closely" />
        <LegendTier fill="var(--color-accent)" shape="dot" label="Other" />
      </div>
    </div>
  );
}

// ── Légende helper ────────────────────────────────────────────────────────────
function LegendTier({
  fill,
  fillOpacity = 1,
  shape,
  label,
}: {
  fill: string;
  fillOpacity?: number;
  shape: "square" | "dot";
  label: string;
}) {
  return (
    <span className="flex items-center gap-1">
      <span
        style={{
          width: shape === "square" ? 12 : 9,
          height: shape === "square" ? 9 : 9,
          borderRadius: shape === "square" ? 2 : "50%",
          background: fill,
          opacity: fillOpacity,
          border: "1px solid var(--color-hairline)",
          display: "inline-block",
          flexShrink: 0,
        }}
      />
      <span className="font-mono text-[9px] text-faint">{label}</span>
    </span>
  );
}
