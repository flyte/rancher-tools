.PHONY: build run

build:
	docker build . --tag rancher-tools

run:
	docker run -ti --rm -v ${HOME}/.rancher/cli.json:/root/.rancher/cli.json:ro rancher-tools
