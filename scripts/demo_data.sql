-- Synthetic local demo only. Re-running inserts missing IDs; existing rows stay unchanged.
CREATE DATABASE IF NOT EXISTS snapshot_source CHARACTER SET utf8mb4;
USE snapshot_source;

CREATE TABLE IF NOT EXISTS demo_customers (
    customer_id INT PRIMARY KEY,
    customer_name VARCHAR(100) NOT NULL,
    region VARCHAR(20) NOT NULL,
    note TEXT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS demo_orders (
    order_id INT PRIMARY KEY,
    customer_id INT NOT NULL,
    ordered_at DATETIME(6) NOT NULL,
    amount DECIMAL(12,2) NOT NULL,
    status VARCHAR(20) NOT NULL,
    note TEXT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS demo_order_items (
    order_id INT NOT NULL,
    line_no INT NOT NULL,
    product_name VARCHAR(100) NOT NULL,
    quantity INT NOT NULL,
    unit_price DECIMAL(12,2) NOT NULL,
    PRIMARY KEY (order_id, line_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

START TRANSACTION;
INSERT INTO demo_customers (customer_id, customer_name, region, note)
SELECT seq, CONCAT('테스트 고객 ', seq), ELT(1 + MOD(seq, 3), '서울', '부산', '대전'),
       CASE WHEN MOD(seq, 5) = 0 THEN NULL ELSE '합성 데이터 😀' END
FROM seq_1_to_100 AS s
WHERE NOT EXISTS (SELECT 1 FROM demo_customers AS d WHERE d.customer_id = s.seq);

INSERT INTO demo_orders (order_id, customer_id, ordered_at, amount, status, note)
SELECT seq, 1 + MOD(seq - 1, 100),
       TIMESTAMP('2026-09-01 09:00:00.123456') + INTERVAL seq MINUTE,
       1000.50 + MOD(seq, 50) * 100, ELT(1 + MOD(seq, 3), '접수', '완료', '취소'),
       CASE WHEN MOD(seq, 7) = 0 THEN NULL ELSE CONCAT('테스트 주문 ', seq, CHAR(10), '두 번째 줄') END
FROM seq_1_to_1200 AS s
WHERE NOT EXISTS (SELECT 1 FROM demo_orders AS d WHERE d.order_id = s.seq);

INSERT INTO demo_order_items (order_id, line_no, product_name, quantity, unit_price)
SELECT s.seq, line.seq, CONCAT('테스트 상품 ', line.seq), 1, 500.25 + MOD(s.seq, 50) * 50
FROM seq_1_to_1200 AS s CROSS JOIN seq_1_to_2 AS line
WHERE NOT EXISTS (
    SELECT 1 FROM demo_order_items AS d WHERE d.order_id = s.seq AND d.line_no = line.seq
);
COMMIT;

SELECT 'demo_customers' AS table_name, COUNT(*) AS row_count FROM demo_customers
UNION ALL SELECT 'demo_orders', COUNT(*) FROM demo_orders
UNION ALL SELECT 'demo_order_items', COUNT(*) FROM demo_order_items;
