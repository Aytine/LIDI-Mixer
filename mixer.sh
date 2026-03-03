#!/bin/bash

# Récupérer le dossier exact où se trouve le script
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# Créer l'environnement virtuel s'il n'existe pas
if [ ! -d "venv" ]; then
    echo "Création de l'environnement virtuel..."
    python3 -m venv venv
fi

# Activer le venv et installer les dépendances
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Lancer l'application
echo "Lancement du MIDI Mixer..."
python3 main.py