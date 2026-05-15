FROM python:3.12
RUN pip install docker

RUN pip install "arkitekt-next[all]>=0.39.5"

RUN pip install kabinet rekuest-next unlok-next rath

RUN mkdir /app
WORKDIR /app
COPY .arkitekt_next /app/.arkitekt_next
COPY . /app

