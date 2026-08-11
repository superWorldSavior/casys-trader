# How-to — Configurer le rendu du cockpit dans le terminal

> **Type** : How-to (Diátaxis) — procédure d'affichage locale.
> **Cockpit** : thème unique `casys`, sombre et ambre.

Le cockpit n'a plus de sélecteur de thème runtime. Il enregistre uniquement le
thème Textual `casys` et utilise `PALETTE_CASYS` pour ses rendus. La touche `d`
ne change donc rien ; `t` masque ou réaffiche la colonne secondaire sur les
écrans compacts.

La transparence et le flou éventuels appartiennent au **terminal**, pas au
trader. Ils modifient tout le fond de la fenêtre et n'ont aucun effet sur les
décisions, les logs ou le daemon.

## Régler le terminal, si souhaité

### iTerm2

Dans **Settings → Profiles → Window** :

1. régler `Transparency` autour de 10 à 15 % ;
2. activer `Blur` si le compositor le permet ;
3. garder un contraste suffisant pour les états rouge/vert et le texte gris.

### Ghostty

Dans `~/.config/ghostty/config` :

```ini
background-opacity = 0.88
background-blur-radius = 20
```

Relancer ou recharger Ghostty après modification.

### Alacritty

Dans sa configuration YAML :

```yaml
window:
  opacity: 0.88
```

Alacritty applique l'opacité ; le flou dépend du système et du compositor.

## Lancer et vérifier

```bash
make watch
```

`make watch` lance le cockpit uniquement. Depuis le preflight ou le cockpit,
appuyer sur `s` pour lancer le daemon supervisé. Pour l'exploitation détachée,
voir [Lancer / relancer / arrêter le daemon](run-the-daemon.md).

Vérifier les huit pages avec les touches `1` à `8`, puis `?` pour la liste des
raccourcis. Si les contrastes deviennent insuffisants, remonter l'opacité du
terminal : il n'existe volontairement pas de palette alternative dans l'app.

## Voir aussi

- [Référence Cockpit](../reference/cockpit.md)
- [Lire les logs](read-logs.md)
