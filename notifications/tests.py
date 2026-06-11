from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from notifications.models import SystemMessage
from notifications.services import (
    close_message,
    create_system_message,
    mark_message_processed,
    mark_message_read,
    refresh_due_snoozed_messages,
    snooze_message,
)
from system.models import PendingEvent
from system.services import process_pending_events


class NotificationServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="notice", password="x")

    def test_mark_message_read_and_processed(self):
        message = create_system_message(self.user.id, "测试消息")

        read_result = mark_message_read(message.id, self.user.id)
        processed_result = mark_message_processed(message.id, self.user.id)

        self.assertTrue(read_result.success)
        self.assertTrue(processed_result.success)
        message.refresh_from_db()
        self.assertEqual(message.status, SystemMessage.Status.PROCESSED)

    def test_close_message(self):
        message = create_system_message(self.user.id, "可关闭消息")

        result = close_message(message.id, self.user.id)

        self.assertTrue(result.success)
        message.refresh_from_db()
        self.assertEqual(message.status, SystemMessage.Status.CLOSED)

    def test_snooze_message_and_refresh_due_message(self):
        message = create_system_message(self.user.id, "稍后提醒消息")

        result = snooze_message(message.id, self.user.id, timezone.now() + timedelta(hours=1))

        self.assertTrue(result.success)
        message.refresh_from_db()
        self.assertEqual(message.status, SystemMessage.Status.SNOOZED)
        self.assertIsNotNone(message.snoozed_until)
        SystemMessage.objects.filter(id=message.id).update(snoozed_until=timezone.now() - timedelta(minutes=1))

        updated = refresh_due_snoozed_messages(self.user.id)

        self.assertEqual(updated, 1)
        message.refresh_from_db()
        self.assertEqual(message.status, SystemMessage.Status.UNREAD)
        self.assertIsNone(message.snoozed_until)

    def test_process_pending_events_marks_success_and_creates_message(self):
        PendingEvent.objects.create(
            event_type="demo",
            idempotency_key="demo:1",
            payload={"operator_id": self.user.id},
        )

        result = process_pending_events()

        self.assertTrue(result.success)
        self.assertEqual(PendingEvent.objects.get().status, PendingEvent.EventStatus.SUCCESS)
        self.assertTrue(SystemMessage.objects.filter(receiver=self.user).exists())


class NotificationViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="notice-view", password="x")
        self.other = User.objects.create_user(username="notice-other", password="x")
        self.message = create_system_message(
            receiver_id=self.user.id,
            title="欠料已齐套",
            content="销售订单可以创建生产指令单",
            source_doc_type="sales_order",
            source_doc_id=1,
            source_doc_no="SO001",
            action_url="/sales/orders/1/",
            suggested_action="创建生产指令单",
        )

    def test_message_list_links_to_detail(self):
        self.client.force_login(self.user)

        response = self.client.get("/notifications/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "欠料已齐套")
        self.assertContains(response, f"/notifications/{self.message.id}/")
        self.assertContains(response, "标记已处理")

    def test_message_list_filters_by_query_status_and_level(self):
        other_message = create_system_message(
            receiver_id=self.user.id,
            title="普通消息",
            content="不应显示",
        )
        other_message.level = SystemMessage.Level.INFO
        other_message.status = SystemMessage.Status.PROCESSED
        other_message.save(update_fields=["level", "status"])
        self.message.level = SystemMessage.Level.URGENT
        self.message.status = SystemMessage.Status.UNREAD
        self.message.save(update_fields=["level", "status"])
        self.client.force_login(self.user)

        response = self.client.get("/notifications/?q=齐套&status=unread&level=urgent")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "欠料已齐套")
        self.assertNotContains(response, "普通消息")
        self.assertContains(response, '<option value="unread" selected>', html=False)
        self.assertContains(response, '<option value="urgent" selected>', html=False)

    def test_message_bulk_action_marks_selected_processed(self):
        second = create_system_message(receiver_id=self.user.id, title="第二条")
        other_message = create_system_message(receiver_id=self.other.id, title="别人的消息")
        self.client.force_login(self.user)

        response = self.client.post(
            "/notifications/bulk-action/",
            {"message_ids": [str(self.message.id), str(second.id), str(other_message.id)], "action": "process"},
        )

        self.assertEqual(response.status_code, 302)
        self.message.refresh_from_db()
        second.refresh_from_db()
        other_message.refresh_from_db()
        self.assertEqual(self.message.status, SystemMessage.Status.PROCESSED)
        self.assertEqual(second.status, SystemMessage.Status.PROCESSED)
        self.assertEqual(other_message.status, SystemMessage.Status.UNREAD)

    def test_message_bulk_action_closes_selected_messages(self):
        second = create_system_message(receiver_id=self.user.id, title="第二条")
        self.client.force_login(self.user)

        response = self.client.post(
            "/notifications/bulk-action/",
            {"message_ids": [str(self.message.id), str(second.id)], "action": "close"},
        )

        self.assertEqual(response.status_code, 302)
        self.message.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.message.status, SystemMessage.Status.CLOSED)
        self.assertEqual(second.status, SystemMessage.Status.CLOSED)

    def test_message_bulk_action_snoozes_selected_messages(self):
        second = create_system_message(receiver_id=self.user.id, title="第二条")
        self.client.force_login(self.user)

        response = self.client.post(
            "/notifications/bulk-action/",
            {"message_ids": [str(self.message.id), str(second.id)], "action": "snooze_one_hour"},
        )

        self.assertEqual(response.status_code, 302)
        self.message.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.message.status, SystemMessage.Status.SNOOZED)
        self.assertEqual(second.status, SystemMessage.Status.SNOOZED)
        self.assertGreater(self.message.snoozed_until, timezone.now())

    def test_snoozed_messages_are_hidden_by_default_but_visible_when_filtered(self):
        self.message.status = SystemMessage.Status.SNOOZED
        self.message.snoozed_until = timezone.now() + timedelta(hours=1)
        self.message.save(update_fields=["status", "snoozed_until"])
        self.client.force_login(self.user)

        default_response = self.client.get("/notifications/")
        filtered_response = self.client.get("/notifications/?status=snoozed")

        self.assertEqual(default_response.status_code, 200)
        self.assertNotContains(default_response, "欠料已齐套")
        self.assertEqual(filtered_response.status_code, 200)
        self.assertContains(filtered_response, "欠料已齐套")

    def test_due_snoozed_message_is_restored_on_list_open(self):
        self.message.status = SystemMessage.Status.SNOOZED
        self.message.snoozed_until = timezone.now() - timedelta(minutes=1)
        self.message.save(update_fields=["status", "snoozed_until"])
        self.client.force_login(self.user)

        response = self.client.get("/notifications/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "欠料已齐套")
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SystemMessage.Status.UNREAD)
        self.assertIsNone(self.message.snoozed_until)

    def test_message_detail_marks_unread_message_read(self):
        self.client.force_login(self.user)

        response = self.client.get(f"/notifications/{self.message.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "创建生产指令单")
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SystemMessage.Status.READ)
        self.assertIsNotNone(self.message.read_at)

    def test_message_process_view_marks_processed(self):
        self.client.force_login(self.user)

        response = self.client.post(f"/notifications/{self.message.id}/process/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/notifications/{self.message.id}/")
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SystemMessage.Status.PROCESSED)

    def test_message_close_view_marks_closed(self):
        self.client.force_login(self.user)

        response = self.client.post(f"/notifications/{self.message.id}/close/")

        self.assertEqual(response.status_code, 302)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SystemMessage.Status.CLOSED)

    def test_message_snooze_view_marks_snoozed(self):
        self.client.force_login(self.user)

        response = self.client.post(
            f"/notifications/{self.message.id}/snooze/",
            {"option": "snooze_one_hour"},
        )

        self.assertEqual(response.status_code, 302)
        self.message.refresh_from_db()
        self.assertEqual(self.message.status, SystemMessage.Status.SNOOZED)
        self.assertGreater(self.message.snoozed_until, timezone.now())

    def test_other_user_cannot_open_message(self):
        self.client.force_login(self.other)

        response = self.client.get(f"/notifications/{self.message.id}/")

        self.assertEqual(response.status_code, 404)
