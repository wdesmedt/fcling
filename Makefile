.PHONY: proto test lint

# The gNMI stubs are generated from the openconfig protos and committed, so
# installing fcli needs no protoc.
proto:
	uv run python -m grpc_tools.protoc -I src --python_out=src --pyi_out=src --grpc_python_out=src \
		src/fcli/gnmi/proto/gnmi.proto src/fcli/gnmi/proto/gnmi_ext.proto

test:
	uv run pytest -q

lint:
	uv run ruff check src tests
	uv run mypy src
