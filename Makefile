PYTEST_OPTS=--timeout=600 -v --reruns=3
ifneq ($(PYTEST_PAR),)
PYTEST_OPTS += -n=$(PYTEST_PAR)
endif


GOPATH = $(shell pwd)/src/lnd
PWD = $(shell pwd)

src/eclair:
	git clone https://github.com/ACINQ/eclair.git src/eclair

src/lightning:
	git clone --recurse-submodules https://github.com/ElementsProject/lightning.git src/lightning

src/lnd:
	git clone https://github.com/lightningnetwork/lnd ${GOPATH}/src/github.com/lightningnetwork/lnd

src/lpd:
	git clone --recurse-submodules https://github.com/LightningPeach/lpd.git src/lpd -b rpc
	cd src/lpd && mkdir -p python_binding && \
		python -m grpc_tools.protoc -I./rpc-server/src --python_out=python_binding --grpc_python_out=python_binding rpc-server/src/{common,routing,channel,payment}.proto

src/ptarmigan:
	git clone https://github.com/nayutaco/ptarmigan.git src/ptarmigan
	cd src/ptarmigan/; git checkout development

update: src/eclair src/lightning src/lnd src/ptarmigan
	rm src/eclair/version src/lightning/version src/lnd/version src/ptarmigan/version || true

	cd src/eclair && git stash; git pull origin master
	cd src/lightning && git stash; git pull origin master
	cd ${GOPATH}/src/github.com/lightningnetwork/lnd && git stash; git pull origin master
	cd src/ptarmigan && git stash; git pull origin development

	#cd src/eclair; git apply ${PWD}/src/eclair/*.patch

bin/eclair.jar: src/eclair
	(cd src/eclair; git rev-parse HEAD) > src/eclair/version
	(cd src/eclair/; mvn package -Dmaven.test.skip=true || true)
	cp src/eclair/eclair-node/target/eclair-node-*-$(shell git --git-dir=src/eclair/.git rev-parse HEAD | cut -b 1-7).jar bin/eclair.jar

bin/lightningd: src/lightning
	(cd src/lightning; git rev-parse HEAD) > src/lightning/version
	cd src/lightning; ./configure --enable-developer --disable-valgrind && make CC=clang
	cp src/lightning/lightningd/lightningd src/lightning/lightningd/lightning_* bin

bin/ptarmd: src/ptarmigan
	(cd src/ptarmigan; git rev-parse HEAD) > src/ptarmigan/version
	cd src/ptarmigan; sed -i -e "s/ENABLE_DEVELOPER_MODE=0/ENABLE_DEVELOPER_MODE=1/g" options.mak
	cd src/ptarmigan; sed -i -e "s/ENABLE_PLOG_TO_STDOUT_PTARMD=0/ENABLE_PLOG_TO_STDOUT_PTARMD=1/g" options.mak
	cd src/ptarmigan; make full
	cp src/ptarmigan/install/ptarmd bin
	cp src/ptarmigan/install/showdb bin
	cp src/ptarmigan/install/routing bin

bin/lnd: src/lnd
	(cd ${GOPATH}/src/github.com/lightningnetwork/lnd; git rev-parse HEAD) > src/lnd/version
	go get -u github.com/golang/dep/cmd/dep
	cd ${GOPATH}/src/github.com/lightningnetwork/lnd; ${GOPATH}/bin/dep ensure; go install . ./cmd/...
	cp ${GOPATH}/bin/lnd ${GOPATH}/bin/lncli bin/

bin/lpd: src/lpd
	(cd src/lpd; git rev-parse HEAD) > src/lpd/version
	cd src/lpd && cargo build --release --package rpc-server
	cp src/lpd/target/release/rpc-server bin/lpd

clean:
	rm src/lnd/version src/lightning/version src/eclair/version src/ptarmigan/version src/lpd/version || true
	rm bin/* || true
	@cd src/lightning && make clean || true
	@cd src/eclair && mvn clean || true
	@cd src/ptarmigan && make distclean || true
	@cd src/lpd && cargo clean || true

clients: bin/lightningd bin/lnd bin/eclair.jar bin/ptarmd bin/lpd

test:
	# Failure is always an option
	py.test -v test.py ${PYTEST_OPTS} --json=report.json || true
	python cli.py postprocess

site:
	rm -rf output/*; rm templates/*.json || true
	cp reports/* templates/
	python cli.py html

push:
	cd output; \
	git init;\
	git config user.name "Travis CI";\
	git config user.email "decker.christian+travis@gmail.com";\
	git add .;\
	git commit --quiet -m "Deploy to GitHub Pages";\
	git push --force "git@github.com:cdecker/lightning-integration.git" master:gh-pages
