# Apify's Python base image. Pinned to 3.13 rather than "latest": a base image
# that moves under us is how a build that passed last week fails today.
FROM apify/actor-python:3.13

COPY requirements.txt ./

RUN echo "Python version:" \
 && python --version \
 && echo "Installing dependencies:" \
 && pip install --no-cache-dir -r requirements.txt \
 && echo "All installed Python packages:" \
 && pip freeze

COPY . ./

# Compile on build so a syntax error fails the build rather than the run.
RUN python -m compileall -q src

CMD ["python3", "-m", "src"]
