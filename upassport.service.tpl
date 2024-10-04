[Unit]
Description=UPasport - UPlanet 54321 - Service
After=network.target

[Service]
User=_USER_
Group=_USER_
WorkingDirectory=_MY_PATH_
ExecStart=/usr/bin/python3 _MY_PATH_/54321.py
Restart=always

[Install]
WantedBy=multi-user.target