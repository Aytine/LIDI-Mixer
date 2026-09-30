"""Application-wide constants and defaults."""

APP_NAME = "MIDI Mixer"
SINGLETON_NAME = "midi-mixer-singleton"
NO_PORT_LABEL = "Aucun port détecté"
DEFAULT_PORT_HINT = "apc40"  # profil par défaut : premier port MIDI dont le nom contient "APC40"
# Profil APC40 par défaut : le fader Master de l'APC40 envoie le CC 14 (canal 1)
DEFAULT_MAPPINGS = {"14": "MASTER"}
LEARN_TIMEOUT_S = 8
# Contrôles APC40 par défaut d'un groupe (colonne i, canal MIDI i+1) :
# fader de piste (CC 7), bouton S / Solo-Cue (note 49) = mute, pad du haut de la grille de clips (note 0) = assign.
# Les boutons S / ● sont en mode bascule : un appui = UN message, en alternant note_on / note_off.
DEFAULT_GROUP_COUNT = 8
GROUP_DEFAULT_CC = 7
GROUP_DEFAULT_MUTE_NOTE = 49
GROUP_DEFAULT_ASSIGN_NOTE = 0
# Couleur de la LED d'Assign : nom -> (vélocité de la palette RGB de l'APC40 mkII, couleur d'aperçu dans l'interface)
ASSIGN_COLORS = {
    "Vert": (21, "#2ecc71"),
    "Rouge": (5, "#e74c3c"),
    "Orange": (9, "#e67e22"),
    "Jaune": (13, "#f1c40f"),
    "Cyan": (37, "#22d3ee"),
    "Bleu": (45, "#3a7ebf"),
    "Violet": (53, "#9b59b6"),
    "Rose": (57, "#ff6fae"),
    "Blanc": (3, "#ecf0f1"),
}
DEFAULT_ASSIGN_COLOR = "Vert"
PORT_POLL_MS = 3000
