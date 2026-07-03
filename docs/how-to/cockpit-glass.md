# Thème casys-glass — évocation Liquid Glass

Le thème `casys-glass` donne au cockpit un rendu sombre avec tuiles semi-transparentes et
bordures arrondies colorées. L'effet « verre dépoli » est porté par le terminal (blur de
fenêtre) — le cockpit lui-même fournit le fond charbon (`#141617`) et les calques alpha.

## Activer

Appuyer sur `d` depuis l'écran principal. Le cycle est :

```
casys-ink  →  casys-glass  →  casys-salmon  →  (retour ink)
             (ce thème)
```

## Configurer le blur terminal

### iTerm2

1. Préférences → Profils → Window
2. **Transparency** : ~15 %
3. **Blur** : activer, rayon ~20

### Ghostty

Ajouter dans `~/.config/ghostty/config` :

```
background-opacity = 0.85
background-blur-radius = 20
```

### Alacritty / autres

```yaml
window:
  opacity: 0.88
```

(Le blur dépend du compositor système — sur macOS avec `yabai` ou Aerospace, le fond du bureau transparaît.)

## Lancer le cockpit

```bash
make watch        # daemon + cockpit
# ou
uv run python -m trader.cockpit
```

## Notes

- En l'absence de blur terminal, le thème ressemble à `casys-ink` avec un fond légèrement
  plus sombre (`#141617` vs `#1d2021`).
- La palette Rich (couleurs des tableaux, statuts) est identique à `casys-ink` : `PALETTE_INK`.
- Les sélecteurs CSS conditionnels `.glass HomePane #home-*` et `.glass #home-flux` appliquent
  `background: #1d2021 35%` et `border: round #8ec07c 40%` sur les tuiles de la home.
