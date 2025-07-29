[Unit]
Description=UPasport - UPlanet 54321 - Service
After=network.target

[Service]
User=_USER_
Group=_USER_
Environment="PATH=%h/.local/bin:%h/.astro/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
WorkingDirectory=_MY_PATH_
ExecStart=_PYTHON_ _MY_PATH_/54321.py
Restart=always

[Install]
WantedBy=multi-user.target
