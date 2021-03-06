from django.db import models
from settings import *
from dateutil import parser
import re
from datetime import timedelta
from mail import timedelta_to_days
from django.template.defaultfilters import slugify

KAGAN_ID = 60
MAX_SLUG_LENGTH = 50

def slugify_unique(model, s):
    s = slugify(s).strip()
    if len(s)>MAX_SLUG_LENGTH:
        s = s[:MAX_SLUG_LENGTH]
        
    # ensure uniqueness
    i = 1
    orig_s = s
    while model.objects.filter(slug=s).count()>0:
        i += 1
        s = orig_s[:(MAX_SLUG_LENGTH - (len(str(i)) + 1))]
        s = "%s-%d" % (s, i)
    
    return s

class Box(models.Model):
    """ An email 'box' (as defined by the ARMS system) """
    def __unicode__(self):
        return "%s (%d)" % (self.name, self.number)
        
    class Meta:
        verbose_name = 'Email Box'
        ordering = ['number']
    
    name = models.CharField("Name", max_length=255)
    number = models.IntegerField("Number")


class PersonManager(models.Manager):
    def elena_kagan(self):
        return Person.objects.get(id=KAGAN_ID)

class Person(models.Model):
    """ A sender or recipient of emails """
    def __unicode__(self):
        return "%s (%s) [%d]" % (self.name, self.alias, self.id)
        
    class Meta:
        verbose_name = 'Person'
        ordering = ['name_hash']
        
    def is_kagan(self):
        return self.id==KAGAN_ID
        
    def save(self):
        self.name_hash = Email.objects.make_name_hash(self.name)
        self.is_merged = not(self.merged_into is None)
        self.slug = slugify_unique(Person, self.name)
        super(Person, self).save()                
    
    name = models.CharField("Name", max_length=255, blank=True, default='')
    name_hash = models.CharField("Name", max_length=255, blank=True, default='')    
    alias = models.CharField("Alias", max_length=255, blank=True, default='')
    
    is_merged = models.BooleanField("Is Merged", default=False)
    merged_into = models.ForeignKey('Person', null=True, blank=True, default=None)
    
    slug = models.SlugField("Slug", default='', blank=True)
    
    objects = PersonManager()


class Thread(models.Model):
    """ An email thread """
    def __unicode__(self):
        return "%s (%d)" % (self.name, self.count)
        
    class Meta:
        verbose_name = "Email Thread"
        ordering = ['name']
    
    def save(self):
        # do some stat-calculation prior to save
        emails = Email.objects.filter(email_thread=self).order_by('creation_date_time')

        if emails.count()>0:
            self.creator = emails[0].creator

        if emails.count()>1:
            cmax = None
            cmin = None
            cavg = timedelta(0, 0)
            for i in range(0, emails.count()-1):
                diff = emails[i+1].creation_date_time - emails[i].creation_date_time
                if (cmax is None) or (diff > cmax):
                    cmax = diff
                if (cmin is None) or (diff < cmin):
                    cmin = diff
                cavg += diff
            
            self.avg_interval = str(timedelta_to_days(cavg) / (1.0 * (emails.count()-1)))
            self.max_interval = str(timedelta_to_days(cmax))
            self.min_interval = str(timedelta_to_days(cmin))

        self.count = emails.count()

        self.slug = slugify_unique(Thread, self.name)

        super(Thread, self).save()

    
    def all_recipient_html(self):
        recipients = {}
        emails = Email.objects.filter(email_thread=self)
        for e in emails:
            for recipient_type in ('to', 'cc'):                
                e_recipients = e.recipient_list(recipient_type)
                for er in e_recipients:
                    recipients[er[1]] = er
        sorted_recipients = sorted(recipients.items(), key=lambda x: x[1][1].upper())
        html_pieces =  map(lambda x: x[1][2], sorted_recipients)
        if len(html_pieces)>8:
            return html_pieces[0] + ' .. ' + Email.NAME_SEPARATOR.join(html_pieces[-6:])
        return Email.NAME_SEPARATOR.join(html_pieces)
    
    name = models.CharField("Name", max_length=255)
    date = models.DateTimeField("Date")
    count = models.IntegerField("Email Count")
    avg_interval = models.DecimalField("Average Interval", max_digits=10, decimal_places=3, blank=True, null=True)
    max_interval = models.DecimalField("Maximum Interval", max_digits=10, decimal_places=3, blank=True, null=True)
    min_interval = models.DecimalField("Minimum Interval", max_digits=10, decimal_places=3, blank=True, null=True)
    star_count = models.IntegerField('Star Count', default=0)
    creator = models.ForeignKey(Person, blank=True, null=True, default=None)
    
    slug = models.SlugField("Slug", default='', blank=True)
    
    merged_into = models.ForeignKey('Thread', null=True, blank=True, default=None)
    
class EmailManager(models.Manager):

    def _compile_re(self, d, abbrev):
        for k in d:
            d[k] = re.compile(d[k].replace('_XX_', abbrev))
        return d

    def __init__(self):
        self.count = 0 # target looks to be 4777; email splitting currently at 4766
        self.success = 0
        self.failure = 0
        
        name_chars = r'[A-Za-z_\s\.\'\-\,]'
        self.re_gremlins = re.compile(r'[\xb0\xb1\xb2\xb3\xb4\xb5\xb6\xb7\xab\xbb\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xae]')
        self.re_non_alpha = re.compile(r'[^A-Z]')
        self.re_re = re.compile(r'([^\s]*re\:\s?)+', re.I)
        self.re_to = {
            'detector': r'^.?\s*.?_XX_:',
            'first': r'(%s+)\s+\(?\s*[cC]N[\~;:=](%s+[=\/])' % (name_chars, name_chars),
            'second': r'^.?\s*.?_XX_:\s+(%s+)\s{3,}' % name_chars,
            'third': r'^.?\s*.?_XX_:\s+"?(%s+)"?\s*[\<\(\@]' % name_chars,
            'fourth': r'^.?\s*.?_XX_:\s+(%s+)' % name_chars,
            # 'fifth': r'^.?\s*.?_XX_:\s+"?(%s+)"?\s*[\<\@]' % name_chars,
            'alias_ats': r'^.?\s*.?_XX_:\s+([A-Z_\s]+)\s+\([\w\@]',   
            'combo': r'([\w\s\.\'\-]+)\s+([A-Z_\s]+)'         
        }
        self.re_cc = self.re_to.copy()
        self.re_bcc = self.re_to.copy()
        self.re_creator = self.re_to.copy()

        self.re_to = self._compile_re(self.re_to, 'TO')
        self.re_cc = self._compile_re(self.re_cc, 'CC')
        self.re_bcc = self._compile_re(self.re_bcc, 'BCC')
        self.re_creator = self._compile_re(self.re_creator, 'CREATOR')

        self.re_subject = re.compile(r'^\s*.?SUBJECT:(.*)')
        self.re_creation_date = re.compile(r'CREATI(O|0|o\.)N\s+[DO]AT[eE][j\/\!]TIME:(.+)')
        self.re_attachment_start = re.compile(r'^([^=;]*)[=:;\s]{5,}ATTACHMENT')
        self.re_attachment_end = re.compile(r'^([^=;]*)[=;\s]{5,}END\s+ATTACHMENT')        
        
        super(EmailManager, self).__init__()

    def make_name_hash(self, t):
        return self.re_non_alpha.sub('',t.strip().upper())

    def make_subject_hash(self, t):
        return self.re_re.sub('', t.strip())

    def grab_name_and_alias(self, line, regex=None):

        if regex is None:
            regex = self.re_to

        line_s = line.strip().replace('"','')

        name = None
        alias = None
        m = regex['first'].search(line_s)
        if m is not None:
            name = m.group(1).strip()
        else:
            m = regex['second'].search(line_s)
            if m is not None:
                if m.group(1).strip().upper()==m.group(1).strip():
                    alias = m.group(1).strip()
                else:
                    name = m.group(1).strip()
            else:
                m = regex['third'].search(line_s)
                if m is not None:
                    if m.group(1).strip().upper()==m.group(1).strip():
                        alias = m.group(1).strip()
                    else:
                        name = m.group(1).strip()
                else:
                    m = regex['fourth'].search(line_s)
                    if m is not None:
                        if m.group(1).strip().upper()==m.group(1).strip():
                            alias = m.group(1).strip()
                        else:
                            name = m.group(1).strip()
                    else:
                        # m = regex['fifth'].search(line_s)
                        # if m is not None:
                        #     name = m.group(1)
                        # else:
                        m = regex['alias_ats'].search(line_s)
                        if m is not None:
                            alias = m.group(1).strip()

        return (name, alias)

    def _extract_person(self, line, regex=None):

        if regex is None:
            regex = self.re_to

        (name, alias) = self.grab_name_and_alias(line, regex)
              
        p = None
        if name:
            m = regex['combo'].search(name)
            if m is not None:
                name = m.group(1).strip()
                alias = m.group(2).strip()
        
            (p, created) = Person.objects.get_or_create(name=name, defaults={'alias': (alias is not None) and alias or '', 'name_hash': self.make_name_hash(name)})
            if (not created) and len(p.alias)==0 and alias:
                p.alias = alias
                p.save()
            
            return p
            
        elif alias:
            try:
                (p, created) = Person.objects.get_or_create(alias=alias)
            except Person.MultipleObjectsReturned, e:
                p = Person.objects.filter(alias=alias)[0]
            return p
            
        else:
            return None
            self.failure += 1
                        
    def _zap_gremlins(self, t):
        return self.re_gremlins.sub("", t)
          
    
    def _clean_date(self, t):
    
        def _trans(d):
            return d.replace('199S','1998').replace('199B','1998').replace('l', '1').replace('i','1').replace('I','1').replace('B', '8').replace('O', '0').replace('S', '5')
        t = t.replace('0CT', 'OCT').replace('5EP', 'SEP').replace('~', '-').replace("'",'').replace(': ',':')
        parts = t.split('-')
        parts[0] = _trans(parts[0])
        if len(parts)>=3:
            parts[2] = _trans(parts[2])
        t = '-'.join(parts)
        return t
  
    def parse_text(self, source_id, source_file, lines):
        self.count += 1
        
        candidate_date = ''
        found_creator = False
        found_to = False
        found_date = False
        found_subject = False
        found_text = False
        collecting_text = False
        collecting_attachment = False
        
        E = Email()        
        E_to = []
        E_cc = []
        E.source = unicode(self._zap_gremlins("".join(lines)), 'utf-8')
        E.source_id = source_id
        E.source_file = source_file
        
        for line in lines:

            # creator
            if (not found_creator) and (self.re_creator['detector'].search(line) is not None):
                p = self._extract_person(line, regex=self.re_creator)
                if p is not None:
                    E.creator = p
                    found_creator = True            

            # to
            if self.re_to['detector'].search(line) is not None:
                p = self._extract_person(line)
                if p is not None:
                    E_to.append(p)
                    found_to = True

            # cc
            if self.re_cc['detector'].search(line) is not None:
                p = self._extract_person(line, regex=self.re_cc)
                if p is not None:
                    E_cc.append(p)

            # creation date/time
            m = self.re_creation_date.search(line)
            if (not found_date) and (m is not None):
                candidate_date = m.group(2).strip()
                candidate_date = self._clean_date(candidate_date)
                try:
                    E.creation_date_time = parser.parse(candidate_date)
                    found_date = True
                except:
                    pass

            # subject
            m = self.re_subject.search(line)
            if (not found_subject) and (m is not None):
                E.subject = unicode(self._zap_gremlins(m.group(1).strip()), 'utf-8')
                E.subject_hash = self.make_subject_hash(E.subject)
                found_subject = True
            
            # text
            if line.strip()=="TEXT:":
                collecting_text = True
                found_text = True
            
            # attachment
            m = self.re_attachment_start.search(line)
            if m is not None:
                collecting_text = False
                collecting_attachment = True
                E.text += unicode(self._zap_gremlins(m.group(1)), 'utf-8')
            
            # end of attachment    
            if self.re_attachment_end.search(line) is not None:
                collecting_attachment = False
                
            if collecting_text: 
                if line.strip()!="TEXT:": # don't collect the text marker itself
                    E.text += unicode(self._zap_gremlins(line), 'utf-8')
                
            if collecting_attachment:
                E.attachment += unicode(self._zap_gremlins(line), 'utf-8')
                
                
        if found_to and found_subject and found_text and found_date and found_creator:            
            E.subject = E.subject.strip()
            E.text = E.text.strip()
            E.attachment = E.attachment.strip()
            
            try:
                E.save()
            except Exception, e:                
                print str(E.source)
                raise e

            E.to = E_to
            E.cc = E_cc
            E.save()

            self.success += 1

            return True

        else:
            f = open('parsed/failures/%s_%s.error' % (source_file, source_id), 'w')
            f.write("found_creator: %s\n" % str(found_creator))
            f.write("found_to: %s\n" % str(found_to))
            f.write("found_subject: %s\n" % str(found_subject))
            f.write("found_text: %s\n" % str(found_text))
            f.write("found_date: %s\n" % str(found_date))
            f.write("candidate date: %s\n\n" % candidate_date)
            f.write(E.source)
            f.close()
            
            return False
        

class Email(models.Model):
    """ An email """
    
    NAME_SEPARATOR = ', '
    
    def __unicode__(self):
        return '%s (%s)' % (self.subject, self.creation_date_time)
        
    class Meta:
        verbose_name = 'Email'
        ordering = ['-creation_date_time']
        
    def recipient_list(self, attr_name):
        h = []
        for p in getattr(self, attr_name).values():
            p_id = p.get('id', 0)
            p_slug = p.get('slug','')
            if len(p_slug)<=3:
                p_slug = str(p['id'])
            p_name = len(p.get('name', '').strip())>0 and p['name'] or p['alias']
            p_html = '<a href="/contact/%s/">%s</a>' % (p_slug, p_name)
            h.append((p_id, p_name, p_html))
        return h
    
    def creator_html(self):
        p = self.creator
        p_name = len(p.name.strip())>0 and p.name or p.alias
        p_slug = len(p.slug.strip())>3 and p.slug or str(p.id)
        return '<a href="/contact/%s/">%s</a>' % (p_slug, p_name)
        
        
    def to_html(self):
        return self.NAME_SEPARATOR.join(map(lambda x: x[2], self.recipient_list('to')))

    def cc_html(self):
        return self.NAME_SEPARATOR.join(map(lambda x: x[2], self.recipient_list('cc')))

    def all_recipient_html(self):
        return self.NAME_SEPARATOR.join(map(lambda x: x[2], self.recipient_list('to'), lambda x: x[2]) + map(lambda x: x[2], self.recipient_list('cc')))
    
    box = models.ForeignKey(Box, blank=True, null=True)
    record_type = models.CharField("Record Type", max_length=200, default='', blank=True)
    creator = models.ForeignKey(Person, related_name='creators', blank=True, null=True)
    creation_date_time = models.DateTimeField("Creation Date/Time", db_index=True, blank=True, null=True)
    subject = models.CharField("Subject", max_length=255, default='', blank=True)
    subject_hash = models.CharField("Subject Hash", max_length=255, default='', blank=True)
    to = models.ManyToManyField(Person, related_name='to', blank=True)
    cc = models.ManyToManyField(Person, null=True, related_name='cc')
    text = models.TextField('Text', default='', blank=True)
    attachment = models.TextField('Attachment', default='', blank=True)
    source = models.TextField('Source', default='', blank=True)
    star_count = models.IntegerField('Star Count', default=0)

    source_file = models.CharField("Source File", max_length=255, default='', blank=True)
    source_id = models.IntegerField("Source ID")
    email_thread = models.ForeignKey('Thread', null=True, blank=True)
    in_reply_to = models.ForeignKey('Email', null=True, blank=True)

    objects = EmailManager()

        
        
