.PHONY: setup fixtures build server demo eval test

setup:
	pip install -r requirements.txt

fixtures:
	python3 scripts/generate_scenarios.py

build:
	cd toolserver && go build -o toolserver .

server: build
	./toolserver/toolserver -scenarios scenarios

demo:
	python3 -m harness investigate INC-1042 --mode llm
	python3 -m harness investigate INC-1045 --mode llm

eval:
	python3 -m harness eval --mode llm

test:
	python3 -m tests.test_agent_loop