#!/usr/bin/env bash

yum install gcc

# Build and install frotz
pushd /tmp
if [ -d "frotz" ]; then rm -Rf frotz; fi
git clone https://gitlab.com/DavidGriffith/frotz.git
pushd frotz
make dumb
make install_dfrotz
popd
popd

install gopher-z.service /etc/systemd/system

systemctl daemon-reload
systemctl enable gopher-z.service
systemctl start gopher-z.service