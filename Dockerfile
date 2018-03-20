FROM python:3.6-alpine3.7

RUN pip install pipenv ipython

WORKDIR /app
COPY Pipfile* ./

RUN pipenv install --system --deploy

COPY rancher_tools.py ./

CMD ["ipython", "-i", "-c", "import rancher_tools as rt"]
