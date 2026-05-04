.PHONY: run start stop down logs

CONTAINER_ID_FILE := .container_id

run:
	./launch_unsloth.sh | tee $(CONTAINER_ID_FILE)

start:
	docker start $$(cat $(CONTAINER_ID_FILE))

stop:
	docker stop $$(cat $(CONTAINER_ID_FILE))

down:
	docker stop $$(cat $(CONTAINER_ID_FILE)) && docker rm $$(cat $(CONTAINER_ID_FILE)) && rm -f $(CONTAINER_ID_FILE)

logs:
	docker logs -f $$(cat $(CONTAINER_ID_FILE))
