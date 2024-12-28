#!/usr/bin/env python3

import sys
import subprocess
from moviepy.editor import VideoFileClip

def split_video(input_file, silence_file, output_dir):
    # Lire les timestamps des silences
    with open(silence_file, 'r') as f:
        silences = [float(line.split()[0]) for line in f]

    # Ajouter le début (0) et la fin de la vidéo
    video = VideoFileClip(input_file)
    silences = [0] + silences + [video.duration]

    # Découper la vidéo
    for i in range(len(silences) - 1):
        start = silences[i]
        end = silences[i+1]

        # Ignorer les segments trop courts (moins de 5 secondes)
        if end - start < 5:
            continue

        output_file = f"{output_dir}/segment_{i:03d}.mp4"

        # Utiliser FFmpeg pour découper la vidéo
        cmd = [
            "ffmpeg",
            "-i", input_file,
            "-ss", str(start),
            "-to", str(end),
            "-c", "copy",
            output_file
        ]

        subprocess.run(cmd, check=True)

    video.close()

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python split_video.py <input_video> <silence_file> <output_dir>")
        sys.exit(1)

    input_file = sys.argv[1]
    silence_file = sys.argv[2]
    output_dir = sys.argv[3]

    split_video(input_file, silence_file, output_dir)
