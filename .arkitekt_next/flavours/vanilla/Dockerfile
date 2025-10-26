FROM python:3.11-slim-buster

RUN pip install docker

RUN pip install "arkitekt-next[all]>=0.30"
RUN pip install "kabinet>=0.9.0"

RUN mkdir /app
WORKDIR /app
COPY .arkitekt_next /app/.arkitekt_next
COPY . /app

