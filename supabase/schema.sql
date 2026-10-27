-- Supabase SQL Editor에서 한 번 실행하세요.

create table if not exists public.iot_sensor_data (
  id          bigserial primary key,
  device_id   text        not null,
  temperature real,
  analog      integer,
  rssi        integer,
  created_at  timestamptz not null default now()
);
create index if not exists iot_sensor_data_device_time on public.iot_sensor_data (device_id, created_at desc);

create table if not exists public.iot_device_state (
  device_id  text primary key,
  led        boolean     not null default false,
  updated_at timestamptz not null default now()
);

insert into public.iot_device_state (device_id, led) values ('xiao-01', false)
on conflict (device_id) do nothing;

-- 백엔드(service_role 키)만 접근하도록 RLS 켜고 정책은 두지 않음
alter table public.iot_sensor_data  enable row level security;
alter table public.iot_device_state enable row level security;
