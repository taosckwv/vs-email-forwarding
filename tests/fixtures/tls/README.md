# 测试 TLS 证书（仅用于进程内替身）

- 放什么：`ca.pem`（测试 CA）、`server.pem` / `server.key`（服务器证书与私钥，SAN = `IP:127.0.0.1`、`DNS:localhost`）。只用于 `tests/integration/` 的进程内 TLS 替身，**不是真实凭据**。
- 不放什么：任何真实主机的证书或私钥；不要拿这份 CA 去签真实服务。
- 有效期：`2026-10-17` 至 `2036-10-14`（10 年）。证书必须带 SAN 与 SKID/AKID——Python 3.13 的严格校验缺任一项会报 `Missing Authority Key Identifier` 或主机名不匹配（实测）。
- 重生成（在 `tests/fixtures/tls/` 下执行；`ca.key` 用完即删、不入库）：

```bash
openssl genrsa -out ca.key 2048
openssl req -new -x509 -key ca.key -out ca.pem -days 3650 -subj "/CN=vs-email-forwarding tests CA/O=vs-email-forwarding tests" -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" -addext "subjectKeyIdentifier=hash" -addext "authorityKeyIdentifier=keyid:always"
openssl genrsa -out server.key 2048
openssl req -new -key server.key -out server.csr -subj "/CN=localhost/O=vs-email-forwarding tests"
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out server.pem -days 3650 -extfile server_ext.cnf -extensions v3_server
rm -f ca.key server.csr ca.srl
```

`server_ext.cnf` 的内容（生成后不留）：

```ini
[v3_server]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = IP:127.0.0.1, DNS:localhost
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
```
