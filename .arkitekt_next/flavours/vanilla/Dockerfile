FROM python:3.11-slim-buster

RUN pip install docker

RUN pip install "arkitekt-next[all, unlok]>=0.12.1"

RUN mkdir /app
WORKDIR /app
COPY .arkitekt_next /app/.arkitekt_next
COPY . /app

