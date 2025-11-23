FROM python:3.11-slim-buster

RUN pip install docker

RUN pip install "arkitekt-next[all]>=0.33"
RUN pip install "kabinet>=0.10.0"

RUN mkdir /app
WORKDIR /app
COPY .arkitekt_next /app/.arkitekt_next
COPY . /app

