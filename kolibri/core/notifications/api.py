from django.db import transaction
from le_utils.constants import content_kinds

from .models import HelpReason
from .models import LearnerProgressNotification
from .models import NotificationEventType
from .models import NotificationObjectType
from .utils import memoize
from kolibri.core.content.models import ContentNode
from kolibri.core.exams.models import ExamAssignment
from kolibri.core.lessons.models import Lesson
from kolibri.core.logger.models import AttemptLog
from kolibri.core.logger.models import ContentSummaryLog


@memoize
def get_assignments(instance, summarylog, attempt=False):
    """
    Returns all Lessons assigned to the user having the content_id
    """
    memberships = instance.user.memberships.all()
    # If the user is not in any classroom nor group, nothing to notify
    if not memberships:
        return []

    content_id = summarylog.content_id
    channel_id = summarylog.channel_id
    learner_groups = [m.collection for m in memberships]

    # Return only active Lessons that are assigned to the requesting user's groups
    filtered_lessons = Lesson.objects.filter(
        lesson_assignments__collection__in=learner_groups,
        is_active=True,
        resources__regex=r"" + content_id + ""
    ).distinct()
    # get the contentnode_id for each lesson:
    lesson_contentnode = {
        lesson.id: r['contentnode_id']
        for lesson in filtered_lessons
        for r in lesson.resources
        if (r['content_id'] == content_id and r['channel_id'] == channel_id)
    }
    if attempt:
        # This part is for the NeedsHelp event. These Events can only be triggered on Exercises:
        to_delete = []
        for lesson_id, contentnode_id in lesson_contentnode.items():
            content_node = ContentNode.objects.get(pk=contentnode_id)
            if content_node.kind != content_kinds.EXERCISE:
                to_delete.append(lesson_id)
        for lesson_id in to_delete:
            del lesson_contentnode[lesson_id]
    # Returns all the affected lessons with the touched contentnode_id, Resource must be inside a lesson
    lesson_resources = [(lesson, lesson_contentnode[lesson.id]) for lesson in filtered_lessons if lesson.id in lesson_contentnode]
    return lesson_resources


def get_exam_group(memberships, exam_id):
    """
    Returns all classrooms or learner groups having this exam
    """
    learner_groups = [m.collection for m in memberships]
    filtered_exam_assignments = ExamAssignment.objects.filter(
        exam_id=exam_id,
        collection__in=learner_groups
    ).distinct()
    touched_groups = [assignment.collection_id for assignment in filtered_exam_assignments]
    return touched_groups


def save_notifications(notifications):
    with transaction.atomic():
        for notification in notifications:
            notification.save()


def create_notification(notification_object, notification_event, user_id, group_id, lesson_id=None,
                        contentnode_id=None,
                        quiz_id=None, reason=None):
    notification = LearnerProgressNotification()
    notification.user_id = user_id
    notification.classroom_id = group_id
    notification.notification_object = notification_object
    notification.notification_event = notification_event
    if contentnode_id:
        notification.contentnode_id = contentnode_id
    if lesson_id:
        notification.lesson_id = lesson_id
    if quiz_id:
        notification.quiz_id = quiz_id
    if reason:
        notification.reason = reason
    return notification


def check_and_created_completed_resource(lesson, user_id, contentnode_id):
    notification = None
    # Check if the notification has been previously saved:
    if not LearnerProgressNotification.objects.filter(user_id=user_id,
                                                      notification_object=NotificationObjectType.Resource,
                                                      notification_event=NotificationEventType.Completed,
                                                      lesson_id=lesson.id,
                                                      contentnode_id=contentnode_id).exists():
        # Let's create an Resource Completion notification
        notification = create_notification(NotificationObjectType.Resource,
                                           NotificationEventType.Completed,
                                           user_id,
                                           lesson.collection_id,
                                           lesson_id=lesson.id,
                                           contentnode_id=contentnode_id)
    return notification


def check_and_created_completed_lesson(lesson, user_id):
    notification = None
    # Check if the notification has been previously saved:
    if not LearnerProgressNotification.objects.filter(user_id=user_id,
                                                      notification_object=NotificationObjectType.Lesson,
                                                      notification_event=NotificationEventType.Completed,
                                                      lesson_id=lesson.id,
                                                      classroom_id=lesson.collection_id).exists():
        # Let's create an Lesson Completion notification
        notification = create_notification(NotificationObjectType.Lesson, NotificationEventType.Completed,
                                           user_id,
                                           lesson.collection_id, lesson_id=lesson.id)
    return notification


def check_and_created_started(lesson, user_id, contentnode_id):
    # If the Resource started notification exists, nothing to do here:
    if LearnerProgressNotification.objects.filter(user_id=user_id,
                                                  notification_object=NotificationObjectType.Resource,
                                                  notification_event=NotificationEventType.Started,
                                                  lesson_id=lesson.id,
                                                  contentnode_id=contentnode_id).exists():
        return []

    notifications = []
    # Let's create an Resource Started notification
    notifications.append(create_notification(NotificationObjectType.Resource,
                                             NotificationEventType.Started,
                                             user_id,
                                             lesson.collection_id,
                                             lesson_id=lesson.id,
                                             contentnode_id=contentnode_id))

    # Check if the Lesson started  has already been created:
    if not LearnerProgressNotification.objects.filter(user_id=user_id,
                                                      notification_object=NotificationObjectType.Lesson,
                                                      notification_event=NotificationEventType.Started,
                                                      lesson_id=lesson.id,
                                                      classroom_id=lesson.collection_id).exists():
        # and create it if that's not the case
        notifications.append(create_notification(NotificationObjectType.Lesson, NotificationEventType.Started,
                                                 user_id,
                                                 lesson.collection_id, lesson_id=lesson.id))
    return notifications


def create_summarylog(summarylog):
    """
    Method called by the ContentSummaryLogSerializer when the
    summarylog is created.
    It creates the Resource and, if needed, the Lesson Started event
    """
    lessons = get_assignments(summarylog, summarylog)
    notifications = []
    for lesson, contentnode_id in lessons:
        notifications_started = check_and_created_started(lesson, summarylog.user_id, contentnode_id)
        notifications += notifications_started

    save_notifications(notifications)


def parse_summarylog(summarylog):
    """
    Method called by the ContentSummaryLogSerializer everytime the
    summarylog is updated.
    It creates the Resource Completed notification.
    It also checks if the Lesson is completed to create the
    Lesson Completed notification.
    """

    if summarylog.progress < 1.0:
        return

    lessons = get_assignments(summarylog, summarylog)
    notifications = []
    for lesson, contentnode_id in lessons:
        # Now let's check completed resources and lessons:
        notification_completed = check_and_created_completed_resource(lesson, summarylog.user_id, contentnode_id)
        if notification_completed:
            notifications.append(notification_completed)
        else:
            continue
        lesson_content_ids = [resource['content_id'] for resource in lesson.resources]

        # Let's check if an LessonResourceIndividualCompletion needs to be created
        user_completed = ContentSummaryLog.objects.filter(user_id=summarylog.user_id,
                                                          content_id__in=lesson_content_ids,
                                                          progress=1.0).count()
        if user_completed == len(lesson_content_ids):
            notification_completed = check_and_created_completed_lesson(lesson, summarylog.user_id)
            if notification_completed:
                notifications.append(notification_completed)

    save_notifications(notifications)


@memoize
def exist_exam_notification(user_id, exam_id):
    return LearnerProgressNotification.objects.filter(user_id=user_id,
                                                      quiz_id=exam_id,
                                                      notification_event=NotificationEventType.Started).exists()


def created_quiz_notification(examlog, event_type):
    user_classrooms = examlog.user.memberships.all()

    touched_groups = get_exam_group(user_classrooms, examlog.exam_id)
    notifications = []
    for group in touched_groups:
        notification = create_notification(NotificationObjectType.Quiz, event_type,
                                           examlog.user_id, group, quiz_id=examlog.exam_id)
        notifications.append(notification)

    save_notifications(notifications)


def create_examlog(examlog):
    """
    Method called by the ExamLogSerializer when the
    examlog is created.
    It creates the Quiz Started notification
    """
    # Checks to add an 'Started' event
    if exist_exam_notification(examlog.user_id, examlog.exam_id):
        return  # the event has already been triggered
    event_type = NotificationEventType.Started
    exist_exam_notification.cache_clear()
    created_quiz_notification(examlog, event_type)


def parse_examlog(examlog):
    """
    Method called by the ExamLogSerializer everytime the
    summarylog is updated.
    It the exam is finished it creates the Quiz Completed notification
    """
    if not examlog.closed:
        return
    event_type = NotificationEventType.Completed
    created_quiz_notification(examlog, event_type)


def parse_attemptslog(attemptlog):
    """
    Method called by the AttemptLogSerializer everytime the
    attemptlog is updated.
    It more than 3 failed attempts exists, it creates a NeededHelp notification
    for the user & resource
    """
    # This Event should not be triggered when a Learner is interacting with an Exercise outside of a Lesson:
    lessons = get_assignments(attemptlog, attemptlog.masterylog.summarylog, attempt=True)
    if not lessons:
        return
    # get all the atempts log on this exercise:
    failed_interactions = []
    attempts = AttemptLog.objects.filter(masterylog_id=attemptlog.masterylog_id)
    failed_interactions = [failed for attempt in attempts for failed in attempt.interaction_history if failed['correct'] == 0]
    # More than 3 errors in this mastery log:
    needs_help = len(failed_interactions) > 3

    if needs_help:
        notifications = []
        for lesson, contentnode_id in lessons:
            # This Event should be triggered only once
            # TODO: Decide if add a day interval filter, to trigger the event in different days
            if LearnerProgressNotification.objects.filter(user_id=attemptlog.user_id,
                                                          notification_object=NotificationObjectType.Resource,
                                                          notification_event=NotificationEventType.Help,
                                                          lesson_id=lesson.id,
                                                          classroom_id=lesson.collection_id,
                                                          contentnode_id=contentnode_id).exists():
                continue
            notification = create_notification(NotificationObjectType.Resource, NotificationEventType.Help,
                                               attemptlog.user_id, lesson.collection_id,
                                               lesson_id=lesson.id,
                                               contentnode_id=contentnode_id,
                                               reason=HelpReason.Multiple)
            notifications.append(notification)
        save_notifications(notifications)
