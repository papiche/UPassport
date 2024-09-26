#!/bin/bash

# Vérification qu'un argument (chemin du répertoire) a été fourni
if [ $# -eq 0 ]; then
    echo "Usage: $0 <chemin_du_repertoire>"
    exit 1
fi

# Récupération du chemin du répertoire
dir_path="$1"


# Vérification que le répertoire existe
if [ ! -d "$dir_path" ]; then
    echo "Le répertoire $dir_path n'existe pas."
    exit 1
fi

# Initialisation du JSON
json="{\n"

# Fonction pour ajouter les fichiers d'un certain type au JSON
add_files_to_json() {
    local type=$1
    local pattern=$2
    json+="  \"$type\": [\n"
    first=true
    for file in "$dir_path"/*$pattern; do
        if [ -f "$file" ]; then
            if [ "$first" = true ]; then
                first=false
            else
                json+=",\n"
            fi
            json+="    \"$(basename "$file")\""
        fi
    done
    json+="\n  ]"
}

# Ajout des fichiers pour chaque type
add_files_to_json "p2p" ".p2p*.png"
json+=",\n"
add_files_to_json "certin" ".certin*.png"
json+=",\n"
add_files_to_json "certout" ".certout*.png"

# Fermeture du JSON
json+="\n}"

# Écriture du JSON dans le fichier manifest.json
echo -e "$json" > "$dir_path/manifest.json"

echo "Le fichier manifest.json a été créé dans $dir_path"
