-- Target instance only. The app creates snapshot tables during a copy.
CREATE DATABASE IF NOT EXISTS snapshot_target CHARACTER SET utf8mb4;

USE snapshot_target;

-- Demo data for the "날짜별 비교" menu. The app itself creates production snapshot tables.
CREATE TABLE IF NOT EXISTS demo_compare (
    snapshot_date DATE NOT NULL,
    item_id INT NOT NULL,
    item_name VARCHAR(100) NOT NULL,
    quantity INT NOT NULL,
    amount DECIMAL(12, 2) NOT NULL,
    PRIMARY KEY (snapshot_date, item_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2026-09-06: item 2 disappears; item 1 changes; item 4 is new on 2026-09-07.
INSERT IGNORE INTO demo_compare (snapshot_date, item_id, item_name, quantity, amount) VALUES
    ('2026-09-06', 1, '테스트 상품 A', 2, 1000.00),
    ('2026-09-06', 2, '테스트 상품 B', 1, 2000.00),
    ('2026-09-06', 3, '테스트 상품 C', 5, 3000.00),
    ('2026-09-07', 1, '테스트 상품 A (변경)', 3, 1200.00),
    ('2026-09-07', 3, '테스트 상품 C', 5, 3000.00),
    ('2026-09-07', 4, '테스트 상품 D (추가)', 1, 4000.00);
