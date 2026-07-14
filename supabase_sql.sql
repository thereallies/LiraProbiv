-- Таблица пользователей
CREATE TABLE IF NOT EXISTS users (
    telegram_id          BIGINT PRIMARY KEY,
    username             TEXT,
    first_name           TEXT,
    balance              DECIMAL(10,2) DEFAULT 0,
    free_queries_used    INT DEFAULT 0,
    daily_queries_used   INT DEFAULT 0,
    last_usage_date      DATE DEFAULT CURRENT_DATE,
    subscription_end_date TIMESTAMPTZ,
    created_at           TIMESTAMPTZ DEFAULT NOW()
);

-- Планы подписок
CREATE TABLE IF NOT EXISTS subscription_plans (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    duration_days INT NOT NULL,
    price         DECIMAL(10,2) NOT NULL
);

INSERT INTO subscription_plans (name, duration_days, price) VALUES
('1 день',      1,    99),
('7 дней',      7,    399),
('31 день',     31,   599),
('180 дней',    180,  1999),
('365 дней',    365,  3599),
('Навсегда',    36500, 9999)
ON CONFLICT (id) DO NOTHING;

-- История покупок
CREATE TABLE IF NOT EXISTS user_subscriptions (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
    plan_id     INT REFERENCES subscription_plans(id),
    start_date  TIMESTAMPTZ DEFAULT NOW(),
    end_date    TIMESTAMPTZ,
    status      TEXT DEFAULT 'active',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_subscriptions_user_id ON user_subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_end_date ON user_subscriptions(end_date);

-- Функция проверки лимита
CREATE OR REPLACE FUNCTION check_query_limit(p_user_id BIGINT)
RETURNS BOOLEAN AS $$
DECLARE
    v_free_queries_used  INT;
    v_daily_queries_used INT;
    v_last_usage_date    DATE;
    v_subscription_end   TIMESTAMPTZ;
BEGIN
    SELECT free_queries_used, daily_queries_used, last_usage_date, subscription_end_date
    INTO v_free_queries_used, v_daily_queries_used, v_last_usage_date, v_subscription_end
    FROM users WHERE telegram_id = p_user_id;

    IF v_last_usage_date < CURRENT_DATE THEN
        v_daily_queries_used := 0;
    END IF;

    IF v_subscription_end IS NOT NULL AND v_subscription_end > NOW() THEN
        IF v_daily_queries_used < 100 THEN
            UPDATE users
            SET daily_queries_used = v_daily_queries_used + 1,
                last_usage_date = CURRENT_DATE
            WHERE telegram_id = p_user_id;
            RETURN TRUE;
        ELSE
            RETURN FALSE;
        END IF;
    ELSE
        IF v_free_queries_used < 3 THEN
            UPDATE users
            SET free_queries_used = v_free_queries_used + 1,
                last_usage_date = CURRENT_DATE
            WHERE telegram_id = p_user_id;
            RETURN TRUE;
        ELSE
            RETURN FALSE;
        END IF;
    END IF;
END;
$$ LANGUAGE plpgsql;

-- Функция покупки подписки
CREATE OR REPLACE FUNCTION purchase_subscription(p_user_id BIGINT, p_plan_id INT)
RETURNS BOOLEAN AS $$
DECLARE
    v_price    DECIMAL(10,2);
    v_duration INT;
    v_balance  DECIMAL(10,2);
    v_new_end  TIMESTAMPTZ;
BEGIN
    SELECT price, duration_days INTO v_price, v_duration
    FROM subscription_plans WHERE id = p_plan_id;

    SELECT balance INTO v_balance FROM users WHERE telegram_id = p_user_id;
    IF v_balance < v_price THEN
        RETURN FALSE;
    END IF;

    UPDATE users SET balance = balance - v_price WHERE telegram_id = p_user_id;

    SELECT subscription_end_date INTO v_new_end FROM users WHERE telegram_id = p_user_id;
    IF v_new_end IS NULL OR v_new_end < NOW() THEN
        v_new_end := NOW() + (v_duration || ' days')::INTERVAL;
    ELSE
        v_new_end := v_new_end + (v_duration || ' days')::INTERVAL;
    END IF;

    UPDATE users
    SET subscription_end_date = v_new_end,
        daily_queries_used   = 0,
        free_queries_used    = 3
    WHERE telegram_id = p_user_id;

    INSERT INTO user_subscriptions (user_id, plan_id, start_date, end_date, status)
    VALUES (p_user_id, p_plan_id, NOW(), v_new_end, 'active');

    RETURN TRUE;
END;
$$ LANGUAGE plpgsql;