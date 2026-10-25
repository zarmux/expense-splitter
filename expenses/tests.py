from decimal import Decimal
import re

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from .models import Expense, Group, Payment, Profile, Split


User = get_user_model()


class DashboardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alex", password="test-pass-123")
        Profile.objects.create(user=self.user, onboarded=True)
        self.client.force_login(self.user)

    def test_post_logout_clears_the_authenticated_session(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        page = client.get(reverse("home"))
        token = re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()
        )
        self.assertIsNotNone(token)

        response = client.post(
            reverse("account_logout"), {"csrfmiddlewaretoken": token.group(1)}
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", client.session)

    def test_empty_dashboard_renders_navigation_and_empty_states(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recent activity")
        self.assertContains(response, "No expenses yet")
        self.assertContains(response, "All settled up")
        self.assertContains(response, reverse("group_list"))
        self.assertContains(response, reverse("friends"))

    def test_dashboard_uses_current_expense_and_balance_data(self):
        friend = User.objects.create_user(username="sam", password="test-pass-123")
        Profile.objects.create(user=friend, onboarded=True)
        group = Group.objects.create(name="Weekend", created_by=self.user)
        group.members.add(self.user, friend)
        expense = Expense.objects.create(
            group=group,
            paid_by=self.user,
            description="Groceries",
            amount=Decimal("100.00"),
        )
        Split.objects.create(expense=expense, user=self.user, amount=Decimal("50.00"))
        Split.objects.create(expense=expense, user=friend, amount=Decimal("50.00"))

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Groceries")
        self.assertContains(response, "Weekend")
        self.assertContains(response, "₹100.00")
        self.assertContains(response, "₹50.00")
        self.assertContains(response, "sam")
        self.assertContains(response, f'name="user" value="{friend.pk}"')

    def test_dashboard_keeps_recent_activity_compact_without_truncating_history(self):
        group = Group.objects.create(name="Weekend", created_by=self.user)
        group.members.add(self.user)
        for index in range(4):
            Expense.objects.create(
                group=group,
                paid_by=self.user,
                description=f"Expense {index}",
                amount=Decimal("10.00"),
            )

        dashboard = self.client.get(reverse("home"))
        history = self.client.get(reverse("history"))

        self.assertEqual(len(dashboard.context["recent"]), 3)
        self.assertEqual(len(history.context["page"].object_list), 4)

    def test_settlement_action_records_the_balance_payment(self):
        friend = User.objects.create_user(username="sam", password="test-pass-123")
        Profile.objects.create(user=friend, onboarded=True)
        group = Group.objects.create(name="Weekend", created_by=self.user)
        group.members.add(self.user, friend)
        expense = Expense.objects.create(
            group=group,
            paid_by=self.user,
            description="Groceries",
            amount=Decimal("100.00"),
        )
        Split.objects.create(expense=expense, user=self.user, amount=Decimal("50.00"))
        Split.objects.create(expense=expense, user=friend, amount=Decimal("50.00"))

        response = self.client.post(reverse("settle"), {"user": friend.pk})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            Payment.objects.filter(
                from_user=friend, to_user=self.user, amount=Decimal("50.00")
            ).exists()
        )

    def test_primary_authenticated_routes_render(self):
        group = Group.objects.create(name="Trip", created_by=self.user)
        group.members.add(self.user)
        urls = (
            reverse("home"),
            reverse("group_list"),
            reverse("group_create"),
            reverse("group_detail", args=(group.pk,)),
            reverse("history"),
            reverse("friends"),
            reverse("profile"),
        )

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'href="/static/expenses/css/style.css?v=preview-match-2"')


# ---------------------------------------------------------------------------
# Tests for the fixes and the friends / pay changes.
# They read response.context instead of page text, so they keep passing when
# the templates are redesigned.
# ---------------------------------------------------------------------------
from django.core.exceptions import ValidationError  # noqa: E402

from .forms import ExpenseForm, OnboardingForm  # noqa: E402
from .models import Friendship  # noqa: E402
from .services import build_upi_uri, create_equal_split, is_valid_upi_id  # noqa: E402


def make_user(name, onboarded=True, upi_id=""):
    user = User.objects.create_user(username=name, password="test-pass-123")
    Profile.objects.create(user=user, onboarded=onboarded, upi_id=upi_id)
    return user


def shared_expense(payer, other, amount="100.00"):
    group = Group.objects.create(name="Trip", created_by=payer)
    group.members.add(payer, other)
    expense = Expense.objects.create(group=group, paid_by=payer, description="Dinner", amount=Decimal(amount))
    create_equal_split(expense)
    return group, expense


class ExpenseAmountTests(TestCase):
    def setUp(self):
        self.user = make_user("alex")
        self.group = Group.objects.create(name="Trip", created_by=self.user)
        self.group.members.add(self.user)

    def form(self, amount):
        return ExpenseForm({"description": "x", "amount": amount, "paid_by": self.user.pk}, group=self.group)

    def test_rejects_zero_and_negative_amounts(self):
        for amount in ("0", "-50", "-0.01"):
            with self.subTest(amount=amount):
                self.assertFalse(self.form(amount).is_valid())

    def test_accepts_a_normal_amount(self):
        self.assertTrue(self.form("12.50").is_valid())

    def test_splitting_a_non_positive_expense_raises(self):
        bad = Expense.objects.create(group=self.group, paid_by=self.user, description="x", amount=Decimal("-5"))
        with self.assertRaises(ValueError):
            create_equal_split(bad)


class SplitMathTests(TestCase):
    def test_uneven_amount_is_split_to_the_cent_and_adds_up(self):
        a, b, c = make_user("a1"), make_user("b1"), make_user("c1")
        group = Group.objects.create(name="Trip", created_by=a)
        group.members.add(a, b, c)
        expense = Expense.objects.create(group=group, paid_by=a, description="Dinner", amount=Decimal("100.00"))
        create_equal_split(expense)
        shares = sorted(s.amount for s in expense.splits.all())
        self.assertEqual(shares, [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")])
        self.assertEqual(sum(shares), Decimal("100.00"))


class UpiTests(TestCase):
    def test_upi_id_format(self):
        for good in ("rahul@okhdfcbank", "a.b-c_d@ybl"):
            self.assertTrue(is_valid_upi_id(good), good)
        for bad in ("", "not a upi id", "x&am=99999&pa=evil@ybl", "no-at-sign", "a@", "@bank"):
            self.assertFalse(is_valid_upi_id(bad), bad)

    def test_profile_form_rejects_a_bad_upi_id_but_allows_blank(self):
        user = make_user("alex")
        self.assertFalse(OnboardingForm({"username": "alexx", "upi_id": "x&am=1&pa=evil@ybl"}, user=user).is_valid())
        self.assertTrue(OnboardingForm({"username": "alexx", "upi_id": ""}, user=user).is_valid())
        self.assertTrue(OnboardingForm({"username": "alexx", "upi_id": "alex@okhdfcbank"}, user=user).is_valid())

    def test_link_is_exact_and_encodes_names(self):
        uri = build_upi_uri("sam@okaxis", "Sam & Co", Decimal("50"), "Settle up")
        self.assertEqual(uri, "upi://pay?pa=sam@okaxis&pn=Sam%20%26%20Co&am=50.00&cu=INR&tn=Settle%20up")


class FriendsListTests(TestCase):
    def setUp(self):
        self.me = make_user("alex")
        self.client.force_login(self.me)

    def people(self, **params):
        response = self.client.get(reverse("friends"), params)
        self.assertEqual(response.status_code, 200)
        return [u.username for u in response.context["people"]]

    def test_lists_everyone_who_finished_signup_without_searching(self):
        make_user("sam")
        make_user("kim")
        make_user("newbie", onboarded=False)
        self.assertEqual(self.people(), ["kim", "sam"])

    def test_search_narrows_the_list(self):
        make_user("sam")
        make_user("kim")
        self.assertEqual(self.people(q="SA"), ["sam"])

    def test_hides_people_already_connected(self):
        friend, pending, incoming = make_user("sam"), make_user("kim"), make_user("lee")
        make_user("zed")
        Friendship.objects.create(from_user=self.me, to_user=friend, accepted=True)
        Friendship.objects.create(from_user=self.me, to_user=pending)
        Friendship.objects.create(from_user=incoming, to_user=self.me)
        self.assertEqual(self.people(), ["zed"])

    def test_add_accept_and_remove_flow(self):
        sam = make_user("sam")
        self.client.post(reverse("friends"), {"user": sam.pk, "action": "add"})
        self.assertTrue(Friendship.objects.filter(from_user=self.me, to_user=sam, accepted=False).exists())

        self.client.force_login(sam)
        self.client.post(reverse("friends"), {"user": self.me.pk, "action": "accept"})
        self.assertTrue(Friendship.objects.get(from_user=self.me, to_user=sam).accepted)

        self.client.post(reverse("friends"), {"user": self.me.pk, "action": "remove"})
        self.assertFalse(Friendship.between(self.me, sam).exists())

    def test_cannot_befriend_someone_who_has_not_finished_signup(self):
        ghost = make_user("ghost", onboarded=False)
        self.client.post(reverse("friends"), {"user": ghost.pk, "action": "add"})
        self.assertFalse(Friendship.between(self.me, ghost).exists())

    def test_bad_input_returns_400_not_a_crash(self):
        self.assertEqual(self.client.post(reverse("friends"), {"action": "add"}).status_code, 400)
        self.assertEqual(self.client.post(reverse("friends"), {"user": "abc", "action": "add"}).status_code, 400)
        self.assertEqual(self.client.post(reverse("settle"), {}).status_code, 400)


class PayTests(TestCase):
    def setUp(self):
        self.me = make_user("alex")
        self.sam = make_user("sam", upi_id="sam@okaxis")
        self.client.force_login(self.me)

    def test_debtor_gets_a_link_for_the_exact_amount_owed(self):
        shared_expense(self.sam, self.me, "100.00")  # sam paid, so alex owes 50
        response = self.client.get(reverse("pay", args=(self.sam.pk,)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["amount"], Decimal("50.00"))
        self.assertTrue(response.context["upi_valid"])
        self.assertIn("pa=sam@okaxis", response.context["upi_uri"])
        self.assertIn("am=50.00", response.context["upi_uri"])
        self.assertTrue(response.context["qr"].startswith("data:image/png;base64,"))

    def test_no_link_when_the_payee_has_no_upi_id(self):
        kim = make_user("kim")
        shared_expense(kim, self.me, "40.00")
        response = self.client.get(reverse("pay", args=(kim.pk,)))
        self.assertFalse(response.context["upi_valid"])
        self.assertNotIn("upi_uri", response.context)

    def test_a_hostile_stored_upi_id_never_becomes_a_link(self):
        evil = make_user("evil", upi_id="x&am=1&pa=evil@ybl")
        shared_expense(evil, self.me, "40.00")
        response = self.client.get(reverse("pay", args=(evil.pk,)))
        self.assertFalse(response.context["upi_valid"])
        self.assertNotIn("upi_uri", response.context)

    def test_nothing_to_pay_redirects_home(self):
        self.assertRedirects(self.client.get(reverse("pay", args=(self.sam.pk,))), reverse("home"), fetch_redirect_response=False)

    def test_you_cannot_pay_someone_who_owes_you(self):
        shared_expense(self.me, self.sam, "100.00")  # alex paid, sam owes alex
        self.assertRedirects(self.client.get(reverse("pay", args=(self.sam.pk,))), reverse("home"), fetch_redirect_response=False)

    def test_once_settled_the_pay_page_closes(self):
        shared_expense(self.sam, self.me, "100.00")
        self.client.post(reverse("settle"), {"user": self.sam.pk})
        self.assertRedirects(self.client.get(reverse("pay", args=(self.sam.pk,))), reverse("home"), fetch_redirect_response=False)


class CsvExportTests(TestCase):
    def test_formula_like_text_is_defused(self):
        me = make_user("alex")
        self.client.force_login(me)
        group = Group.objects.create(name="Trip", created_by=me)
        group.members.add(me)
        Expense.objects.create(group=group, paid_by=me, description="=HYPERLINK(\"http://evil\")", amount=Decimal("10"))
        body = self.client.get(reverse("history") + "?export=1").content.decode()
        self.assertIn("'=HYPERLINK", body)
        self.assertNotIn(",=HYPERLINK", body)


class GroupAccessTests(TestCase):
    def test_non_members_cannot_open_a_group(self):
        owner, outsider = make_user("alex"), make_user("sam")
        group = Group.objects.create(name="Private", created_by=owner)
        group.members.add(owner)
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(reverse("group_detail", args=(group.pk,))).status_code, 404)


class FriendActionRegressionTests(TestCase):
    def setUp(self):
        self.me = make_user("alex")
        self.client.force_login(self.me)

    def test_search_result_form_posts_the_person_id(self):
        person = make_user("sam")
        response = self.client.get(reverse("friends"), {"q": "sam"})
        self.assertContains(response, f'name="user" value="{person.pk}"')
        self.assertContains(response, 'name="action" value="add"')

    def test_add_from_search_creates_request_for_the_rendered_person(self):
        person = make_user("sam")
        self.client.get(reverse("friends"), {"q": "sam"})
        response = self.client.post(reverse("friends"), {
            "user": str(person.pk), "action": "add",
        })
        self.assertRedirects(response, reverse("friends"), fetch_redirect_response=False)
        self.assertTrue(Friendship.objects.filter(from_user=self.me, to_user=person, accepted=False).exists())

    def test_duplicate_request_does_not_create_a_second_relationship(self):
        person = make_user("sam")
        for _ in range(2):
            self.client.post(reverse("friends"), {"user": person.pk, "action": "add"})
        self.assertEqual(Friendship.between(self.me, person).count(), 1)

    def test_self_friend_request_and_unknown_action_are_rejected(self):
        self.assertEqual(self.client.post(reverse("friends"), {"user": self.me.pk, "action": "add"}).status_code, 400)
        person = make_user("sam")
        self.assertEqual(self.client.post(reverse("friends"), {"user": person.pk, "action": "unknown"}).status_code, 400)
        self.assertFalse(Friendship.between(self.me, person).exists())

    def test_outgoing_request_can_be_cancelled_by_its_sender(self):
        person = make_user("sam")
        friendship = Friendship.objects.create(from_user=self.me, to_user=person)
        response = self.client.post(reverse("friends"), {"user": person.pk, "action": "cancel"})
        self.assertRedirects(response, reverse("friends"), fetch_redirect_response=False)
        self.assertFalse(Friendship.objects.filter(pk=friendship.pk).exists())

    def test_only_an_incoming_pending_request_can_be_accepted(self):
        person = make_user("sam")
        Friendship.objects.create(from_user=self.me, to_user=person)
        self.client.post(reverse("friends"), {"user": person.pk, "action": "accept"})
        self.assertFalse(Friendship.objects.get(from_user=self.me, to_user=person).accepted)
        Friendship.objects.create(from_user=person, to_user=self.me)
        self.client.post(reverse("friends"), {"user": person.pk, "action": "accept"})
        self.assertTrue(Friendship.objects.get(from_user=person, to_user=self.me).accepted)

    def test_friend_profile_is_private_until_connection_is_accepted(self):
        person = make_user("sam")
        self.assertEqual(self.client.get(reverse("friend_profile", args=(person.pk,))).status_code, 404)
        Friendship.objects.create(from_user=self.me, to_user=person, accepted=True)
        response = self.client.get(reverse("friend_profile", args=(person.pk,)))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "sam")

    def test_friend_profile_does_not_expose_email_or_upi(self):
        person = User.objects.create_user(username="sam", email="private@example.test", password="test-pass-123")
        Profile.objects.create(user=person, onboarded=True, upi_id="sam@okaxis")
        Friendship.objects.create(from_user=self.me, to_user=person, accepted=True)
        response = self.client.get(reverse("friend_profile", args=(person.pk,)))
        self.assertNotContains(response, "private@example.test")
        self.assertNotContains(response, "sam@okaxis")


class QuickExpenseRegressionTests(TestCase):
    def setUp(self):
        self.me = make_user("alex")
        self.client.force_login(self.me)

    def test_empty_account_gets_a_create_group_path(self):
        response = self.client.get(reverse("quick_expense"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create a group first")
        self.assertContains(response, reverse("group_create"))

    def test_quick_expense_creates_expense_and_equal_splits(self):
        friend = make_user("sam")
        group = Group.objects.create(name="Weekend", created_by=self.me)
        group.members.add(self.me, friend)
        response = self.client.post(reverse("quick_expense"), {
            "group": group.pk,
            "description": "Dinner",
            "amount": "27.00",
            "paid_by": self.me.pk,
        })
        self.assertRedirects(response, reverse("group_detail", args=(group.pk,)), fetch_redirect_response=False)
        expense = Expense.objects.get(group=group, description="Dinner")
        self.assertEqual(expense.splits.count(), 2)
        self.assertEqual(sum(expense.splits.values_list("amount", flat=True)), Decimal("27.00"))

    def test_quick_expense_rejects_group_outside_membership(self):
        owner, friend = make_user("owner"), make_user("friend")
        group = Group.objects.create(name="Private", created_by=owner)
        group.members.add(owner, friend)
        response = self.client.post(reverse("quick_expense"), {
            "group": group.pk, "description": "Unauthorized", "amount": "10.00", "paid_by": owner.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Expense.objects.filter(description="Unauthorized").exists())

    def test_quick_expense_rejects_payer_not_in_selected_group(self):
        outsider = make_user("outsider")
        group = Group.objects.create(name="My group", created_by=self.me)
        group.members.add(self.me)
        response = self.client.post(reverse("quick_expense"), {
            "group": group.pk, "description": "Wrong payer", "amount": "10.00", "paid_by": outsider.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        self.assertFalse(Expense.objects.filter(description="Wrong payer").exists())

    def test_quick_expense_rejects_zero_amount(self):
        group = Group.objects.create(name="My group", created_by=self.me)
        group.members.add(self.me)
        response = self.client.post(reverse("quick_expense"), {
            "group": group.pk, "description": "Free?", "amount": "0", "paid_by": self.me.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Expense.objects.filter(description="Free?").exists())

    def test_other_users_cannot_use_the_quick_form(self):
        self.client.logout()
        response = self.client.get(reverse("quick_expense"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response.url)


class GroupDeletionRegressionTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.member = make_user("member")
        self.group = Group.objects.create(name="Trip", created_by=self.owner)
        self.group.members.add(self.owner, self.member)
        self.expense = Expense.objects.create(
            group=self.group, paid_by=self.owner, description="Train", amount=Decimal("40.00")
        )
        Split.objects.create(expense=self.expense, user=self.owner, amount=Decimal("20.00"))
        Split.objects.create(expense=self.expense, user=self.member, amount=Decimal("20.00"))

    def test_only_creator_can_delete_and_delete_cascades_group_expenses(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(reverse("group_delete", args=(self.group.pk,))).status_code, 404)
        self.client.force_login(self.owner)
        response = self.client.post(reverse("group_delete", args=(self.group.pk,)))
        self.assertRedirects(response, reverse("group_list"), fetch_redirect_response=False)
        self.assertFalse(Group.objects.filter(pk=self.group.pk).exists())
        self.assertFalse(Expense.objects.filter(pk=self.expense.pk).exists())
        self.assertFalse(Split.objects.filter(expense_id=self.expense.pk).exists())

    def test_deletion_is_post_only_and_group_list_shows_owner_only_control(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("group_delete", args=(self.group.pk,))).status_code, 405)
        response = self.client.get(reverse("group_list"))
        self.assertContains(response, reverse("group_delete", args=(self.group.pk,)))
        self.assertContains(response, "Account-wide settlements are not linked to groups and will remain")
        self.client.force_login(self.member)
        member_page = self.client.get(reverse("group_list"))
        self.assertNotContains(member_page, reverse("group_delete", args=(self.group.pk,)))

    def test_group_delete_does_not_delete_global_payment_records(self):
        payment = Payment.objects.create(from_user=self.member, to_user=self.owner, amount=Decimal("5.00"))
        self.client.force_login(self.owner)
        self.client.post(reverse("group_delete", args=(self.group.pk,)))
        self.assertTrue(Payment.objects.filter(pk=payment.pk).exists())


class FormSecurityRegressionTests(TestCase):
    def test_username_uniqueness_is_case_insensitive(self):
        existing = make_user("Faraz")
        another = make_user("other")
        self.assertFalse(OnboardingForm({"username": "FARAZ", "upi_id": ""}, user=another).is_valid())
        self.assertTrue(OnboardingForm({"username": "fARAz", "upi_id": ""}, user=existing).is_valid())

    def test_group_form_only_offers_accepted_friends(self):
        owner, friend, stranger = make_user("owner"), make_user("friend"), make_user("stranger")
        Friendship.objects.create(from_user=owner, to_user=friend, accepted=True)
        from .forms import GroupForm
        form = GroupForm(user=owner)
        self.assertEqual(list(form.fields["members"].queryset), [friend])
        tampered = GroupForm({"name": "Only friends", "members": [stranger.pk]}, user=owner)
        self.assertFalse(tampered.is_valid())

    def test_group_balance_explains_global_settlement_scope(self):
        owner, friend = make_user("owner"), make_user("friend")
        group = Group.objects.create(name="Trip", created_by=owner)
        group.members.add(owner, friend)
        self.client.force_login(owner)
        response = self.client.get(reverse("group_detail", args=(group.pk,)))
        self.assertContains(response, "Before settlements recorded across all groups")

    def test_social_login_uses_post_form(self):
        self.client.force_login(make_user("alex"))
        self.client.logout()
        response = self.client.get(reverse("account_login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'method="post"')


class PaymentTemplateRegressionTests(TestCase):
    def setUp(self):
        self.me = make_user("alex")
        self.client.force_login(self.me)

    def test_valid_upi_payment_page_renders_qr_and_exact_amount_action(self):
        payee = make_user("sam", upi_id="sam@okaxis")
        shared_expense(payee, self.me, "88.00")
        response = self.client.get(reverse("pay", args=(payee.pk,)))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Open UPI app")
        self.assertContains(response, "₹44.00")
        self.assertContains(response, "data:image/png;base64,")
        self.assertContains(response, "Payment is processed by your UPI app")

    def test_invalid_stored_upi_id_never_renders_a_payment_link(self):
        payee = make_user("evil", upi_id="x&amp=1&pa=evil@ybl")
        shared_expense(payee, self.me, "40.00")
        response = self.client.get(reverse("pay", args=(payee.pk,)))
        self.assertContains(response, "No valid UPI ID saved")
        self.assertNotContains(response, "upi://pay")


class GroupDetailRegressionTests(TestCase):
    def setUp(self):
        self.me = make_user("alex")
        self.friend = make_user("sam")
        self.group = Group.objects.create(name="Dinner", created_by=self.me)
        self.group.members.add(self.me, self.friend)
        self.client.force_login(self.me)

    def test_existing_group_detail_expense_form_still_saves_and_splits(self):
        response = self.client.post(reverse("group_detail", args=(self.group.pk,)), {
            "description": "Dinner",
            "amount": "31.00",
            "paid_by": self.me.pk,
        })
        self.assertRedirects(response, reverse("group_detail", args=(self.group.pk,)), fetch_redirect_response=False)
        expense = Expense.objects.get(group=self.group, description="Dinner")
        self.assertEqual(sum(expense.splits.values_list("amount", flat=True)), Decimal("31.00"))
        self.assertEqual(expense.splits.count(), 2)

    def test_existing_group_detail_form_rejects_a_nonmember_payer(self):
        outsider = make_user("outsider")
        response = self.client.post(reverse("group_detail", args=(self.group.pk,)), {
            "description": "Invalid payer",
            "amount": "12.00",
            "paid_by": outsider.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Expense.objects.filter(description="Invalid payer").exists())
