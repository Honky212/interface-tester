# 用户服务（SOAP/XML）

## POST /soap/user

以 SOAP 1.1 风格调用用户查询服务（**XML 报文**）。

### 请求体（XML）

```xml
<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetUser><userId>1001</userId></GetUser>
  </soap:Body>
</soap:Envelope>
```

### 响应（XML）

```xml
<?xml version="1.0"?>
<Envelope><Body><GetUserResponse><nickname>alice</nickname><level>3</level></GetUserResponse></Body></Envelope>
```

- `//nickname`：字符串，昵称
- `//level`：整数，会员等级

### 业务规则

- userId 不存在时返回 `code: 2001`
