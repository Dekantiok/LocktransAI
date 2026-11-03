from django.urls import path

from agent import views

app_name = 'agent'

urlpatterns = [
    path('', views.chat_page, name='chat'),
    path('send/', views.chat_send, name='chat_send'),
    path('verify/', views.chat_verify, name='chat_verify'),
    path('dismiss/', views.chat_dismiss, name='chat_dismiss'),
    path('voice/', views.chat_voice, name='chat_voice'),
    path('transcribe/', views.transcribe_field, name='transcribe_field'),
    path('new/', views.chat_new, name='chat_new'),
    path('poll/', views.chat_poll, name='chat_poll'),
]
