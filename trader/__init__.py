"""casys-trader — infrastructure de l'agent de trading autonome.

Le daemon (boucle runtime) appelle Codex pour décider, passe par le risk gate,
exécute en paper. La stratégie / les indicateurs / le calendrier de réveil ne
sont PAS codés ici : ils sont définis par l'agent via le mandat et la mémoire.
"""

__version__ = "0.1.0"
