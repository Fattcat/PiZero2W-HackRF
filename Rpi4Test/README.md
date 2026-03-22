```
cd ~/Rpi4HackRF
```

```
python3 -m venv venv
```

```
source venv/bin/activate
```

```
pip install flask numpy scipy flask pydub pyaudioop
```

```
sudo apt install ffmpeg
```

```
mkdir -p static
```

```
mv index.html static/
```

```
python server.py
```
