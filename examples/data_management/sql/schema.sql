-- T4：SQL 造数 / 校验示例用的表结构（MySQL）
--
-- 为什么要单独给 DDL：
--   测试数据管理的另一半在数据库里（造数、校验落库、回收）。约定是
--   **「按运行/用例前缀命名 + 只删自己造的行」**，所以表里必须有能区分批次/用例的列。
--   这里用 `biz_id`（业务唯一 ID，由 debugtalk.unique_id() 生成）承载这个职责。
--
-- 用法（需要一个可连的 MySQL）：
--   mysql -h127.0.0.1 -uroot -p < schema.sql
--   pip install -e ".[sql]"      # sqlalchemy + pymysql
--   hrun examples/data_management/sql/04_sql_data_management.yml
--
-- NOTICE: 未安装 sql extra 时，`sql/` 目录下的用例会被 conftest.py 主动 **skip**
-- （框架本身遇到缺依赖是抛异常/报错，不是 skip）。

CREATE DATABASE IF NOT EXISTS dm_demo DEFAULT CHARSET utf8mb4;

USE dm_demo;

CREATE TABLE IF NOT EXISTS dm_records (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    biz_id      VARCHAR(64)  NOT NULL COMMENT '业务唯一 ID（造数时生成，用于精确定位与回收）',
    run_prefix  VARCHAR(64)  NOT NULL COMMENT '本次运行的前缀（按运行隔离/回收）',
    case_prefix VARCHAR(128) NOT NULL COMMENT '用例前缀（按用例隔离/回收）',
    name        VARCHAR(128) NOT NULL,
    amount      INT          NOT NULL DEFAULT 0,
    created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_biz_id (biz_id),
    KEY idx_run_prefix (run_prefix),
    KEY idx_case_prefix (case_prefix)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;
