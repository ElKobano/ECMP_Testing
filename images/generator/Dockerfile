FROM python:3.11-alpine

ENV LANG=C.UTF-8

RUN    apk update \
    && apk add --no-cache \
       libpcap \
       tcpdump \
       ethtool \
       iptables \
    && python3 -m pip install --no-cache-dir scapy \
    && mkdir /usr/testlibs

COPY  libs/traffic_gen.py /usr/testlibs/

CMD  ["python3", \
      "/usr/testlibs/traffic_gen.py", \
      "--port", \
      "1234", \
      "--host", \
      "0.0.0.0"]
