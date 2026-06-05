"""Boîte à outils de l'agent — primitives composables, contrats étroits.

Chaque module fait UNE chose et expose une interface stable que l'agent runtime
manipule. Les implémentations (data, exécution) sont swappables sans toucher au
reste (ex. SimBroker -> IB).
"""
