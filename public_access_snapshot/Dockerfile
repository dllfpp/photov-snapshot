FROM alpine:3.21

# Chromium from Alpine keeps the image far smaller than a Debian-based browser
# image, which matters on a Raspberry Pi.
RUN apk add --no-cache \
      chromium \
      python3 \
      py3-aiohttp \
      font-noto-emoji \
      ttf-dejavu \
      tzdata \
  && adduser -D -u 10001 photov

ENV PYTHONUNBUFFERED=1 \
    CHROME_BIN=/usr/bin/chromium-browser

COPY snapshot.py /srv/snapshot.py

USER photov
CMD ["python3", "/srv/snapshot.py"]
