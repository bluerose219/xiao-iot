-- Edge Impulse 학습용 이미지 수집. Supabase SQL Editor에서 한 번 실행하세요.

create table if not exists public.iot_images (
  id         uuid primary key default gen_random_uuid(),
  label      text        not null,
  path       text        not null unique,   -- Storage 경로: <label>/<label>.<id>.jpg
  size       integer,
  created_at timestamptz not null default now()
);
create index if not exists iot_images_label_time on public.iot_images (label, created_at desc);
alter table public.iot_images enable row level security;

-- 비공개 버킷 (백엔드가 service 키로만 접근, 대시보드는 서명된 URL로 표시)
insert into storage.buckets (id, name, public)
values ('ei-images', 'ei-images', false)
on conflict (id) do nothing;
